#!/usr/bin/env python3
"""Local Txoko server with SQLite storage and same-origin JSON API."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import http.cookies
import ipaddress
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
import urllib.parse
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
SESSION_COOKIE = "txoko_session"
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
PIN_ITERATIONS = 310_000
MAX_REQUEST_BYTES = 32_768
LOGIN_ATTEMPT_LIMIT = 8
LOGIN_LOCK_SECONDS = 15 * 60
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,24}$")
PIN_RE = re.compile(r"^[0-9]{6,12}$")
LOG = logging.getLogger("txoko")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exception_type: Any, exception: Any, traceback: Any) -> Any:
        try:
            return super().__exit__(exception_type, exception, traceback)
        finally:
            self.close()


def utc_now() -> int:
    return int(time.time())


def clean_name(value: Any, label: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{label} es obligatorio.")
    result = " ".join(value.strip().split())
    if not result or len(result) > max_length or any(ord(char) < 32 for char in result):
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{label} no es válido.")
    return result


def normalize_name(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", value)
    return "".join(char for char in decomposed if unicodedata.category(char) != "Mn").casefold()


def guest_key(first_name: str, last_name: str) -> str:
    identity = f"{normalize_name(first_name)}|{normalize_name(last_name)}".encode("utf-8")
    return hashlib.sha256(identity).hexdigest()


def money_summary(connection: sqlite3.Connection) -> dict[str, int]:
    income = connection.execute(
        """
        SELECT
          COALESCE(SUM(amount_cents), 0) AS total,
          COALESCE(SUM(CASE WHEN status IN ('pending', 'payment_requested')
            THEN amount_cents ELSE 0 END), 0) AS pending,
          COALESCE(SUM(CASE WHEN status = 'paid'
            THEN amount_cents ELSE 0 END), 0) AS collected
        FROM transactions
        """
    ).fetchone()
    expenses = connection.execute(
        """
        SELECT
          COALESCE(SUM(CASE WHEN status = 'approved' THEN amount_cents ELSE 0 END), 0) AS approved,
          COALESCE(SUM(CASE WHEN status = 'pending' THEN amount_cents ELSE 0 END), 0) AS pending
        FROM expenses
        """
    ).fetchone()
    return {
        "totalSalesCents": income["total"],
        "pendingCents": income["pending"],
        "collectedCents": income["collected"],
        "approvedExpensesCents": expenses["approved"],
        "pendingExpensesCents": expenses["pending"],
        "potBalanceCents": income["collected"] - expenses["approved"],
    }


def public_user(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "username": row["username"],
        "firstName": row["first_name"],
        "lastName": row["last_name"],
        "role": row["role"],
        "active": bool(row["active"]),
    }


class TxokoStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.data_dir, 0o700)
        except OSError:
            LOG.exception("Could not restrict the data folder permissions")
        self.database_path = data_dir / "txoko.sqlite3"
        self.setup_token_path = data_dir / ".txoko-admin-token"
        self._token_lock = threading.Lock()
        self._initialize_database()
        if self.needs_initial_president():
            self._ensure_setup_token()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=15,
            isolation_level=None,
            factory=ClosingConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _ensure_setup_token(self, replace: bool = False) -> str | None:
        with self._token_lock:
            if self.setup_token_path.exists() and not replace:
                try:
                    return self.setup_token_path.read_text(encoding="utf-8").strip()
                except OSError:
                    LOG.exception("Could not read the initial administrator token file")
                    return None

            token = secrets.token_urlsafe(24)
            try:
                fd = os.open(self.setup_token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as token_file:
                    token_file.write(token + "\n")
                return token
            except OSError:
                LOG.exception("Could not save the initial administrator token")
                return None

    def reset_setup_token(self) -> str:
        connection = self.connect()
        try:
            if connection.execute("SELECT 1 FROM users WHERE role = 'president' AND active = 1").fetchone():
                raise ApiError(HTTPStatus.CONFLICT, "Ya existe un presidente activo.")
        finally:
            connection.close()
        token = self._ensure_setup_token(replace=True)
        if not token:
            raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "No se pudo guardar el token de configuración.")
        return token

    def _initialize_database(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                  id TEXT PRIMARY KEY,
                  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                  first_name TEXT NOT NULL,
                  last_name TEXT NOT NULL,
                  pin_salt BLOB NOT NULL,
                  pin_hash BLOB NOT NULL,
                  role TEXT NOT NULL CHECK (role IN ('president', 'member', 'former_member')),
                  active INTEGER NOT NULL CHECK (active IN (0, 1)),
                  created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                  token_hash TEXT PRIMARY KEY,
                  user_id TEXT NOT NULL REFERENCES users(id),
                  expires_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS sessions_user_id ON sessions(user_id);
                CREATE TABLE IF NOT EXISTS products (
                  id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  name_key TEXT NOT NULL UNIQUE,
                  stock INTEGER NOT NULL CHECK (stock >= 0),
                  price_cents INTEGER NOT NULL CHECK (price_cents > 0),
                  active INTEGER NOT NULL CHECK (active IN (0, 1)),
                  created_at INTEGER NOT NULL,
                  updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS guests (
                  id TEXT PRIMARY KEY,
                  first_name TEXT NOT NULL,
                  last_name TEXT NOT NULL,
                  normalized_name TEXT NOT NULL,
                  created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transactions (
                  id TEXT PRIMARY KEY,
                  product_id TEXT NOT NULL,
                  item_name TEXT NOT NULL,
                  amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
                  consumer_type TEXT NOT NULL CHECK (consumer_type IN ('member', 'guest')),
                  consumer_uid TEXT,
                  guest_id TEXT,
                  consumer_name TEXT NOT NULL,
                  consumer_last_name TEXT NOT NULL,
                  status TEXT NOT NULL CHECK (status IN ('pending', 'payment_requested', 'paid')),
                  created_at INTEGER NOT NULL,
                  payment_requested_at INTEGER,
                  paid_at INTEGER,
                  confirmed_by TEXT
                );
                CREATE TABLE IF NOT EXISTS expenses (
                  id TEXT PRIMARY KEY,
                  description TEXT NOT NULL,
                  amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
                  requester_id TEXT NOT NULL,
                  requester_name TEXT NOT NULL,
                  status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
                  created_at INTEGER NOT NULL,
                  reviewed_by TEXT,
                  reviewer_name TEXT,
                  reviewed_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS expenses_status_created
                  ON expenses(status, created_at);
                CREATE INDEX IF NOT EXISTS expenses_requester_created
                  ON expenses(requester_id, created_at);
                CREATE INDEX IF NOT EXISTS transactions_member_debts
                  ON transactions(consumer_uid, status, created_at);
                CREATE INDEX IF NOT EXISTS transactions_guest_debts
                  ON transactions(guest_id, status, created_at);
                CREATE TABLE IF NOT EXISTS login_attempts (
                  attempt_key TEXT PRIMARY KEY,
                  failures INTEGER NOT NULL,
                  locked_until INTEGER NOT NULL
                );
                """
            )
        try:
            os.chmod(self.database_path, 0o600)
        except OSError:
            LOG.exception("Could not restrict the database file permissions")

    def needs_initial_president(self) -> bool:
        with self.connect() as connection:
            return connection.execute(
                "SELECT 1 FROM users WHERE role = 'president' AND active = 1 LIMIT 1"
            ).fetchone() is None

    def create_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO sessions(token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                (hashlib.sha256(token.encode()).hexdigest(), user_id, utc_now() + SESSION_TTL_SECONDS),
            )
        return token

    def get_session_user(self, token: str | None) -> sqlite3.Row | None:
        if not token:
            return None
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT users.* FROM sessions
                JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ? AND sessions.expires_at > ? AND users.active = 1
                """,
                (token_hash, utc_now()),
            ).fetchone()
            if row is None:
                connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            return row

    def logout(self, token: str | None) -> None:
        if token:
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            with self.connect() as connection:
                connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def register(self, payload: dict[str, Any]) -> tuple[sqlite3.Row, str]:
        username = payload.get("username")
        if not isinstance(username, str) or not USERNAME_RE.fullmatch(username):
            raise ApiError(HTTPStatus.BAD_REQUEST, "El usuario debe tener entre 3 y 24 letras, números, puntos, guiones o guiones bajos.")
        pin = payload.get("pin")
        if not isinstance(pin, str) or not PIN_RE.fullmatch(pin):
            raise ApiError(HTTPStatus.BAD_REQUEST, "El PIN debe tener entre 6 y 12 números.")
        first_name = clean_name(payload.get("firstName"), "Nombre", 60)
        last_name = clean_name(payload.get("lastName"), "Apellidos", 100)
        setup_token = payload.get("setupToken")
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            president = connection.execute(
                "SELECT id FROM users WHERE role = 'president' AND active = 1 LIMIT 1"
            ).fetchone()
            if president is None:
                saved_token = self.setup_token_path.read_text(encoding="utf-8").strip() if self.setup_token_path.exists() else ""
                if not isinstance(setup_token, str) or not saved_token or not hmac.compare_digest(setup_token, saved_token):
                    raise ApiError(HTTPStatus.FORBIDDEN, "Hace falta el token inicial del presidente. Comprueba la consola de Termux.")
                role = "president"
            else:
                role = "member"

            user_id = secrets.token_hex(16)
            salt = secrets.token_bytes(16)
            pin_hash = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, PIN_ITERATIONS)
            connection.execute(
                """
                INSERT INTO users(id, username, first_name, last_name, pin_salt, pin_hash, role, active, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (user_id, username.strip(), first_name, last_name, salt, pin_hash, role, utc_now()),
            )
            connection.commit()
        except sqlite3.IntegrityError as error:
            connection.rollback()
            raise ApiError(HTTPStatus.CONFLICT, "Ese nombre de usuario ya está en uso.") from error
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        if role == "president":
            try:
                self.setup_token_path.unlink(missing_ok=True)
            except OSError:
                LOG.exception("Could not remove the consumed administrator token")
        return self.get_user(user_id), self.create_session(user_id)

    def get_user(self, user_id: str) -> sqlite3.Row:
        with self.connect() as connection:
            user = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if user is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "No se encuentra el usuario.")
        return user

    def authenticate(self, remote: str, username: Any, pin: Any) -> tuple[sqlite3.Row, str]:
        username_text = username.strip() if isinstance(username, str) else ""
        pin_text = pin if isinstance(pin, str) else ""
        attempt_key = hashlib.sha256(remote.encode()).hexdigest()
        now = utc_now()
        with self.connect() as connection:
            attempt = connection.execute(
                "SELECT failures, locked_until FROM login_attempts WHERE attempt_key = ?",
                (attempt_key,),
            ).fetchone()
            if attempt and attempt["locked_until"] > now:
                wait = attempt["locked_until"] - now
                raise ApiError(HTTPStatus.TOO_MANY_REQUESTS, f"Demasiados intentos. Prueba de nuevo en {wait // 60 + 1} minutos.")
            user = connection.execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
                (username_text,),
            ).fetchone()

        salt = user["pin_salt"] if user else bytes(16)
        expected = user["pin_hash"] if user else bytes(32)
        candidate = hashlib.pbkdf2_hmac("sha256", pin_text.encode(), salt, PIN_ITERATIONS)
        valid_pin = hmac.compare_digest(candidate, expected)
        if not user or not valid_pin or not user["active"]:
            with self.connect() as connection:
                previous = connection.execute(
                    "SELECT failures FROM login_attempts WHERE attempt_key = ?",
                    (attempt_key,),
                ).fetchone()
                failures = (previous["failures"] if previous else 0) + 1
                locked_until = now + LOGIN_LOCK_SECONDS if failures >= LOGIN_ATTEMPT_LIMIT else 0
                connection.execute(
                    """
                    INSERT INTO login_attempts(attempt_key, failures, locked_until) VALUES (?, ?, ?)
                    ON CONFLICT(attempt_key) DO UPDATE SET failures = excluded.failures, locked_until = excluded.locked_until
                    """,
                    (attempt_key, failures, locked_until),
                )
            raise ApiError(HTTPStatus.UNAUTHORIZED, "Usuario o PIN incorrectos.")

        with self.connect() as connection:
            connection.execute("DELETE FROM login_attempts WHERE attempt_key = ?", (attempt_key,))
        return user, self.create_session(user["id"])

    def list_products(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, name, stock, price_cents, active FROM products WHERE active = 1 ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "stock": row["stock"],
                "priceCents": row["price_cents"],
                "active": bool(row["active"]),
            }
            for row in rows
        ]

    def list_debts(self, user: sqlite3.Row | None) -> list[dict[str, Any]]:
        with self.connect() as connection:
            if user and user["role"] == "president":
                rows = connection.execute(
                    """
                    SELECT * FROM transactions
                    WHERE status IN ('pending', 'payment_requested')
                    ORDER BY created_at DESC
                    """
                ).fetchall()
            elif user:
                rows = connection.execute(
                    """
                    SELECT * FROM transactions
                    WHERE consumer_uid = ? AND status != 'paid'
                    ORDER BY created_at DESC
                    """,
                    (user["id"],),
                ).fetchall()
            else:
                rows = []
        return [self.public_transaction(row) for row in rows]

    @staticmethod
    def public_transaction(row: sqlite3.Row) -> dict[str, Any]:
        consumer: dict[str, Any] = {
            "type": row["consumer_type"],
            "name": row["consumer_name"],
            "lastName": row["consumer_last_name"],
        }
        if row["consumer_uid"]:
            consumer["uid"] = row["consumer_uid"]
        if row["guest_id"]:
            consumer["guestId"] = row["guest_id"]
        result: dict[str, Any] = {
            "id": row["id"],
            "itemName": row["item_name"],
            "amountCents": row["amount_cents"],
            "consumer": consumer,
            "status": row["status"],
            "createdAt": {"seconds": row["created_at"]},
        }
        if row["payment_requested_at"]:
            result["paymentRequestedAt"] = {"seconds": row["payment_requested_at"]}
        if row["paid_at"]:
            result["paidAt"] = {"seconds": row["paid_at"]}
        if row["confirmed_by"]:
            result["confirmedBy"] = row["confirmed_by"]
        return result

    def list_guests(self) -> list[dict[str, str]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, first_name, last_name FROM guests ORDER BY normalized_name LIMIT 500"
            ).fetchall()
        return [{"id": row["id"], "firstName": row["first_name"], "lastName": row["last_name"]} for row in rows]

    def guest_debts(self, guest_id: Any) -> list[dict[str, Any]]:
        if not isinstance(guest_id, str) or not re.fullmatch(r"[a-f0-9]{64}", guest_id):
            raise ApiError(HTTPStatus.BAD_REQUEST, "Selecciona un invitado válido.")
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM transactions
                WHERE guest_id = ? AND status IN ('pending', 'payment_requested')
                ORDER BY created_at DESC
                """,
                (guest_id,),
            ).fetchall()
        return [self.public_transaction(row) for row in rows]

    def create_product(self, user: sqlite3.Row, payload: dict[str, Any]) -> dict[str, Any]:
        self.require_president(user)
        name = clean_name(payload.get("name"), "Nombre del producto", 80)
        stock = payload.get("stock")
        price_cents = payload.get("priceCents")
        if type(stock) is not int or not 0 <= stock <= 1_000_000:
            raise ApiError(HTTPStatus.BAD_REQUEST, "La cantidad debe ser un entero entre 0 y 1.000.000.")
        if type(price_cents) is not int or not 1 <= price_cents <= 100_000_000:
            raise ApiError(HTTPStatus.BAD_REQUEST, "El precio debe ser mayor que cero.")
        name_key = normalize_name(name)
        product_id = hashlib.sha256(name_key.encode()).hexdigest()
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
            if existing and existing["active"]:
                connection.rollback()
                raise ApiError(HTTPStatus.CONFLICT, "Ya hay un producto activo con ese nombre.")
            connection.execute(
                """
                INSERT INTO products(id, name, name_key, stock, price_cents, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET name = excluded.name, name_key = excluded.name_key,
                  stock = excluded.stock, price_cents = excluded.price_cents, active = 1, updated_at = excluded.updated_at
                """,
                (product_id, name, name_key, stock, price_cents, now, now),
            )
            connection.commit()
        return {"id": product_id, "name": name, "stock": stock, "priceCents": price_cents, "active": True}

    @staticmethod
    def require_president(user: sqlite3.Row | None) -> None:
        if user is None or user["role"] != "president":
            raise ApiError(HTTPStatus.FORBIDDEN, "Solo el presidente puede realizar esta acción.")

    def adjust_stock(self, user: sqlite3.Row, product_id: str, amount: Any) -> None:
        self.require_president(user)
        if type(amount) is not int or not 1 <= amount <= 1_000_000:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Introduce una cantidad entera mayor que cero.")
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE products SET stock = stock + ?, updated_at = ? WHERE id = ? AND active = 1 AND stock + ? <= 1000000",
                (amount, utc_now(), product_id, amount),
            )
            if cursor.rowcount != 1:
                raise ApiError(HTTPStatus.CONFLICT, "El producto no está disponible o la cantidad supera el límite.")

    def archive_product(self, user: sqlite3.Row, product_id: str) -> None:
        self.require_president(user)
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE products SET active = 0, updated_at = ? WHERE id = ? AND active = 1",
                (utc_now(), product_id),
            )
            if cursor.rowcount != 1:
                raise ApiError(HTTPStatus.NOT_FOUND, "No se encuentra ese producto.")

    def consume(
        self,
        user: sqlite3.Row | None,
        product_id: str,
        guest_id: Any,
        guest_name: Any,
    ) -> dict[str, Any]:
        now = utc_now()
        transaction_id = secrets.token_hex(16)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            product = connection.execute(
                "SELECT * FROM products WHERE id = ? AND active = 1",
                (product_id,),
            ).fetchone()
            if product is None:
                raise ApiError(HTTPStatus.NOT_FOUND, "Este producto ya no está disponible.")
            if product["stock"] < 1:
                raise ApiError(HTTPStatus.CONFLICT, "No quedan unidades de este producto.")

            guest = None
            if user:
                if not user["active"]:
                    raise ApiError(HTTPStatus.FORBIDDEN, "Esta cuenta está desactivada.")
                consumer_type = "member"
                consumer_uid = user["id"]
                resolved_guest_id = None
                first_name = user["first_name"]
                last_name = user["last_name"]
            else:
                consumer_type = "guest"
                consumer_uid = None
                if guest_name is not None:
                    if not isinstance(guest_name, dict):
                        raise ApiError(HTTPStatus.BAD_REQUEST, "El nombre del invitado no es válido.")
                    first_name = clean_name(guest_name.get("firstName"), "Nombre", 60)
                    last_name = clean_name(guest_name.get("lastName"), "Apellidos", 100)
                    resolved_guest_id = guest_key(first_name, last_name)
                    guest = connection.execute("SELECT * FROM guests WHERE id = ?", (resolved_guest_id,)).fetchone()
                    if guest is None:
                        connection.execute(
                            "INSERT INTO guests(id, first_name, last_name, normalized_name, created_at) VALUES (?, ?, ?, ?, ?)",
                            (
                                resolved_guest_id,
                                first_name,
                                last_name,
                                f"{normalize_name(first_name)} {normalize_name(last_name)}",
                                now,
                            ),
                        )
                else:
                    if not isinstance(guest_id, str) or not re.fullmatch(r"[a-f0-9]{64}", guest_id):
                        raise ApiError(HTTPStatus.BAD_REQUEST, "Selecciona o registra un invitado.")
                    guest = connection.execute("SELECT * FROM guests WHERE id = ?", (guest_id,)).fetchone()
                    if guest is None:
                        raise ApiError(HTTPStatus.NOT_FOUND, "No se encuentra ese invitado.")
                    resolved_guest_id = guest_id
                    first_name = guest["first_name"]
                    last_name = guest["last_name"]

            connection.execute("UPDATE products SET stock = stock - 1, updated_at = ? WHERE id = ?", (now, product_id))
            connection.execute(
                """
                INSERT INTO transactions(
                  id, product_id, item_name, amount_cents, consumer_type, consumer_uid, guest_id,
                  consumer_name, consumer_last_name, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    transaction_id,
                    product_id,
                    product["name"],
                    product["price_cents"],
                    consumer_type,
                    consumer_uid,
                    resolved_guest_id,
                    first_name,
                    last_name,
                    now,
                ),
            )
            connection.commit()
        return {"transactionId": transaction_id, "amountCents": product["price_cents"]}

    def request_payment(self, user: sqlite3.Row | None, transaction_id: str, guest_id: Any) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            debt = connection.execute("SELECT * FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
            if debt is None:
                raise ApiError(HTTPStatus.NOT_FOUND, "No se encuentra esa deuda.")
            if debt["status"] != "pending":
                raise ApiError(HTTPStatus.CONFLICT, "Esta deuda ya está avisada o pagada.")
            belongs_to_member = user is not None and debt["consumer_uid"] == user["id"] and user["active"]
            belongs_to_guest = user is None and guest_id and debt["guest_id"] == guest_id
            if not belongs_to_member and not belongs_to_guest:
                raise ApiError(HTTPStatus.FORBIDDEN, "No puedes modificar esta deuda.")
            connection.execute(
                "UPDATE transactions SET status = 'payment_requested', payment_requested_at = ? WHERE id = ?",
                (utc_now(), transaction_id),
            )
            connection.commit()

    def confirm_payment(self, user: sqlite3.Row, transaction_id: str) -> None:
        self.require_president(user)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            debt = connection.execute("SELECT * FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
            if debt is None:
                raise ApiError(HTTPStatus.NOT_FOUND, "No se encuentra esa deuda.")
            if debt["status"] != "payment_requested":
                raise ApiError(HTTPStatus.CONFLICT, "El consumidor aún no ha avisado del pago.")
            connection.execute(
                "UPDATE transactions SET status = 'paid', paid_at = ?, confirmed_by = ? WHERE id = ?",
                (utc_now(), user["id"], transaction_id),
            )
            connection.commit()

    def list_members(self, user: sqlite3.Row) -> list[dict[str, Any]]:
        self.require_president(user)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM users WHERE active = 1 AND role != 'president' ORDER BY first_name, last_name"
            ).fetchall()
        return [public_user(row) for row in rows]

    @staticmethod
    def public_expense(row: sqlite3.Row) -> dict[str, Any]:
        result = {
            "id": row["id"],
            "description": row["description"],
            "amountCents": row["amount_cents"],
            "requesterId": row["requester_id"],
            "requesterName": row["requester_name"],
            "status": row["status"],
            "createdAt": row["created_at"],
            "reviewerId": row["reviewed_by"],
            "reviewerName": row["reviewer_name"],
            "reviewedAt": row["reviewed_at"],
        }
        return result

    def list_expenses(self, user: sqlite3.Row | None) -> list[dict[str, Any]]:
        if user is None or not user["active"]:
            raise ApiError(HTTPStatus.UNAUTHORIZED, "Inicia sesión para consultar los gastos del Txoko.")
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM expenses ORDER BY created_at DESC"
            ).fetchall()
        return [self.public_expense(row) for row in rows]

    def create_expense(self, user: sqlite3.Row | None, payload: dict[str, Any]) -> dict[str, Any]:
        if user is None or not user["active"] or user["role"] == "former_member":
            raise ApiError(HTTPStatus.UNAUTHORIZED, "Inicia sesión con una cuenta activa para registrar un gasto.")
        description = clean_name(payload.get("description"), "Descripción de la compra", 160)
        amount_cents = payload.get("amountCents")
        if type(amount_cents) is not int or not 1 <= amount_cents <= 100_000_000:
            raise ApiError(HTTPStatus.BAD_REQUEST, "El importe debe ser mayor que cero y no superar 1.000.000 €.")

        expense_id = secrets.token_hex(16)
        created_at = utc_now()
        requester_name = f"{user['first_name']} {user['last_name']}"
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO expenses(
                  id, description, amount_cents, requester_id, requester_name, status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (expense_id, description, amount_cents, user["id"], requester_name, created_at),
            )
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone()
        return self.public_expense(row)

    def review_expense(self, user: sqlite3.Row | None, expense_id: Any, approved: bool) -> None:
        self.require_president(user)
        if not isinstance(expense_id, str) or not re.fullmatch(r"[a-f0-9]{32}", expense_id):
            raise ApiError(HTTPStatus.BAD_REQUEST, "El gasto no es válido.")

        reviewed_at = utc_now()
        status = "approved" if approved else "rejected"
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            expense = connection.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone()
            if expense is None:
                raise ApiError(HTTPStatus.NOT_FOUND, "No se encuentra ese gasto.")
            if expense["status"] != "pending":
                raise ApiError(HTTPStatus.CONFLICT, "Este gasto ya ha sido revisado.")
            if approved:
                balance = money_summary(connection)["potBalanceCents"]
                if expense["amount_cents"] > balance:
                    raise ApiError(
                        HTTPStatus.CONFLICT,
                        f"El bote disponible ({balance / 100:.2f} €) no cubre este gasto.",
                    )
            connection.execute(
                """
                UPDATE expenses
                SET status = ?, reviewed_by = ?, reviewer_name = ?, reviewed_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (
                    status,
                    user["id"],
                    f"{user['first_name']} {user['last_name']}",
                    reviewed_at,
                    expense_id,
                ),
            )
            connection.commit()

    def deactivate_member(self, user: sqlite3.Row, member_id: str) -> None:
        self.require_president(user)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            member = connection.execute("SELECT role, active FROM users WHERE id = ?", (member_id,)).fetchone()
            if member is None or member["role"] == "president":
                raise ApiError(HTTPStatus.NOT_FOUND, "No se puede retirar esta cuenta.")
            connection.execute(
                "UPDATE users SET active = 0, role = 'former_member' WHERE id = ?",
                (member_id,),
            )
            connection.execute("DELETE FROM sessions WHERE user_id = ?", (member_id,))
            connection.commit()


class TxokoHandler(SimpleHTTPRequestHandler):
    server_version = "TxokoLocal/1.0"
    store: TxokoStore
    secure_cookie: bool

    def __init__(self, *args: Any, directory: str, store: TxokoStore, secure_cookie: bool, **kwargs: Any):
        self.store = store
        self.secure_cookie = secure_cookie
        super().__init__(*args, directory=directory, **kwargs)

    def log_message(self, format_string: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), format_string % args)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ApiError(HTTPStatus.BAD_REQUEST, "La solicitud no es válida.") from error
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "La solicitud es demasiado grande.")
        try:
            payload = json.loads(self.rfile.read(length) if length else b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ApiError(HTTPStatus.BAD_REQUEST, "El contenido JSON no es válido.") from error
        if not isinstance(payload, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "El contenido de la solicitud no es válido.")
        return payload

    def _session_token(self) -> str | None:
        cookie = http.cookies.SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except http.cookies.CookieError:
            return None
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def _user(self, required: bool = False) -> sqlite3.Row | None:
        user = self.store.get_session_user(self._session_token())
        if required and user is None:
            raise ApiError(HTTPStatus.UNAUTHORIZED, "Inicia sesión para continuar.")
        return user

    def _check_same_origin(self) -> None:
        origin = self.headers.get("Origin")
        if origin:
            parsed = urllib.parse.urlparse(origin)
            host = self.headers.get("Host", "")
            hostname = urllib.parse.urlsplit(f"//{host}").hostname or ""
            try:
                ip = ipaddress.ip_address(hostname.split("%", 1)[0])
                is_local_host = ip.is_private or ip.is_loopback or ip.is_link_local
                if isinstance(ip, ipaddress.IPv4Address):
                    is_local_host = is_local_host or ip in ipaddress.ip_network("100.64.0.0/10")
            except ValueError:
                is_local_host = hostname == "localhost" or hostname.endswith(".localhost") or hostname.endswith(".local")
            if (
                not parsed.netloc
                or parsed.netloc.casefold() != host.casefold()
                or not is_local_host
            ):
                raise ApiError(HTTPStatus.FORBIDDEN, "Origen de solicitud no permitido.")

    def _send_json(self, payload: Any, status: int = HTTPStatus.OK, token: str | None = None, clear_token: bool = False) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; "
            "script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; "
            "base-uri 'self'; frame-ancestors 'none'; form-action 'self'",
        )
        if token is not None:
            flags = "; HttpOnly; SameSite=Strict; Path=/"
            if self.secure_cookie:
                flags += "; Secure"
            self.send_header("Set-Cookie", f"{SESSION_COOKIE}={token}{flags}; Max-Age={SESSION_TTL_SECONDS}")
        elif clear_token:
            flags = "; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
            if self.secure_cookie:
                flags += "; Secure"
            self.send_header("Set-Cookie", f"{SESSION_COOKIE}={flags}")
        self.end_headers()
        self.wfile.write(body)

    def _api_error(self, error: Exception) -> None:
        if isinstance(error, ApiError):
            self._send_json({"error": str(error)}, error.status)
        else:
            LOG.exception("Unhandled API error")
            self._send_json({"error": "Error interno del servidor."}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            super().do_GET()
            return
        try:
            query = urllib.parse.parse_qs(parsed.query)
            if parsed.path == "/api/status":
                self._send_json({"needsPresident": self.store.needs_initial_president()})
            elif parsed.path == "/api/me":
                user = self._user()
                self._send_json({"user": public_user(user) if user else None})
            elif parsed.path == "/api/products":
                self._send_json({"products": self.store.list_products()})
            elif parsed.path == "/api/ledger":
                with self.store.connect() as connection:
                    self._send_json(money_summary(connection))
            elif parsed.path == "/api/debts":
                self._send_json({"debts": self.store.list_debts(self._user())})
            elif parsed.path == "/api/guests":
                self._send_json({"guests": self.store.list_guests()})
            elif parsed.path == "/api/guest-debts":
                self._send_json({"debts": self.store.guest_debts(query.get("guestId", [""])[0])})
            elif parsed.path == "/api/members":
                self._send_json({"members": self.store.list_members(self._user(required=True))})
            elif parsed.path == "/api/expenses":
                self._send_json({"expenses": self.store.list_expenses(self._user(required=True))})
            else:
                raise ApiError(HTTPStatus.NOT_FOUND, "Ruta no encontrada.")
        except Exception as error:
            self._api_error(error)

    def do_POST(self) -> None:
        if not urllib.parse.urlparse(self.path).path.startswith("/api/"):
            self._send_json({"error": "Ruta no encontrada."}, HTTPStatus.NOT_FOUND)
            return
        try:
            self._check_same_origin()
            parsed = urllib.parse.urlparse(self.path)
            payload = self._read_json()
            if parsed.path == "/api/register":
                user, token = self.store.register(payload)
                self._send_json({"user": public_user(user)}, HTTPStatus.CREATED, token=token)
            elif parsed.path == "/api/login":
                user, token = self.store.authenticate(self.client_address[0], payload.get("username"), payload.get("pin"))
                self._send_json({"user": public_user(user)}, token=token)
            elif parsed.path == "/api/logout":
                self.store.logout(self._session_token())
                self._send_json({"ok": True}, clear_token=True)
            elif parsed.path == "/api/products":
                product = self.store.create_product(self._user(required=True), payload)
                self._send_json({"product": product}, HTTPStatus.CREATED)
            elif parsed.path == "/api/consume":
                user = self._user()
                result = self.store.consume(
                    user,
                    payload.get("productId", ""),
                    payload.get("guestId"),
                    payload.get("guestName"),
                )
                self._send_json(result, HTTPStatus.CREATED)
            elif parsed.path == "/api/stock":
                self.store.adjust_stock(
                    self._user(required=True),
                    payload.get("productId", ""),
                    payload.get("amount"),
                )
                self._send_json({"ok": True})
            elif parsed.path == "/api/archive-product":
                self.store.archive_product(self._user(required=True), payload.get("productId", ""))
                self._send_json({"ok": True})
            elif parsed.path == "/api/request-payment":
                self.store.request_payment(
                    self._user(),
                    payload.get("transactionId", ""),
                    payload.get("guestId"),
                )
                self._send_json({"ok": True})
            elif parsed.path == "/api/confirm-payment":
                self.store.confirm_payment(self._user(required=True), payload.get("transactionId", ""))
                self._send_json({"ok": True})
            elif parsed.path == "/api/deactivate-member":
                self.store.deactivate_member(self._user(required=True), payload.get("userId", ""))
                self._send_json({"ok": True})
            elif parsed.path == "/api/expenses":
                expense = self.store.create_expense(self._user(required=True), payload)
                self._send_json({"expense": expense}, HTTPStatus.CREATED)
            elif parsed.path == "/api/approve-expense":
                self.store.review_expense(self._user(required=True), payload.get("expenseId"), approved=True)
                self._send_json({"ok": True})
            elif parsed.path == "/api/reject-expense":
                self.store.review_expense(self._user(required=True), payload.get("expenseId"), approved=False)
                self._send_json({"ok": True})
            else:
                raise ApiError(HTTPStatus.NOT_FOUND, "Ruta no encontrada.")
        except Exception as error:
            self._api_error(error)

    def do_PUT(self) -> None:
        self.do_POST()

    def translate_path(self, path: str) -> str:
        parsed = urllib.parse.urlparse(path)
        decoded = urllib.parse.unquote(parsed.path)
        requested = Path(decoded.lstrip("/"))
        if any(part.startswith(".") for part in requested.parts):
            return str(ROOT / "__not_found__")
        target = (ROOT / requested).resolve()
        if ROOT != target and ROOT not in target.parents:
            return str(ROOT / "__not_found__")
        if target.is_dir():
            target = target / "index.html"
        return str(target)

    def end_headers(self) -> None:
        if self.path.startswith("/api/"):
            super().end_headers()
            return
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; "
            "script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; "
            "base-uri 'self'; frame-ancestors 'none'; form-action 'self'",
        )
        super().end_headers()


def run_server(host: str, port: int, data_dir: Path, secure_cookie: bool) -> None:
    store = TxokoStore(data_dir)
    if store.needs_initial_president():
        token = store._ensure_setup_token()
        print("\nTXOKO: aún no hay presidente.", flush=True)
        print(f"Token inicial para el primer registro: {token}", flush=True)
        print(f"El token se conserva en {store.setup_token_path} hasta usarlo.", flush=True)
    handler = partial(
        TxokoHandler,
        directory=str(ROOT),
        store=store,
        secure_cookie=secure_cookie,
    )
    try:
        server = ThreadingHTTPServer((host, port), handler)
    except OSError as error:
        raise SystemExit(f"No se pudo iniciar el servidor en {host}:{port}: {error}") from error
    server.daemon_threads = True
    print(f"Txoko disponible en http://127.0.0.1:{port}", flush=True)
    if host in ("0.0.0.0", "::"):
        print("Para otros móviles en la misma Wi-Fi, usa la IP local de este teléfono.", flush=True)
    print("Mantén Termux abierto para que el inventario siga disponible. Ctrl+C detiene el servidor.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nCerrando Txoko...", flush=True)
    finally:
        server.server_close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Servidor local de inventario Txoko")
    parser.add_argument("--host", default="127.0.0.1", help="Interfaz de red (por defecto, solo este ordenador)")
    parser.add_argument("--port", type=int, default=8080, help="Puerto HTTP (por defecto: 8080)")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path.home() / ".txoko",
        help="Carpeta privada para la base de datos y el token inicial",
    )
    parser.add_argument("--secure-cookie", action="store_true", help="Marca la cookie Secure si usas HTTPS delante del servidor")
    parser.add_argument("--reset-setup-token", action="store_true", help="Genera otro token inicial si todavía no hay presidente")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args()
    store = TxokoStore(args.data_dir)
    if args.reset_setup_token:
        print(f"Nuevo token inicial: {store.reset_setup_token()}")
        return
    if not 1 <= args.port <= 65535:
        raise SystemExit("El puerto debe estar entre 1 y 65535.")
    run_server(args.host, args.port, args.data_dir, args.secure_cookie)


if __name__ == "__main__":
    main()
