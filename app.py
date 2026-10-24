#!/usr/bin/env python3
"""Vercel FastAPI entrypoint for the Txoko web app."""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
from http import HTTPStatus
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from server import (
    MAX_REQUEST_BYTES,
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    ApiError,
    TxokoStore,
    money_summary,
    public_user,
)


LOG = logging.getLogger("txoko.vercel")
ROOT = Path(__file__).resolve().parent
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def store_for(request: Request) -> TxokoStore:
    store = getattr(request.app.state, "txoko_store", None)
    if store is None:
        if os.environ.get("VERCEL") and not os.environ.get("DATABASE_URL"):
            raise RuntimeError("Configura DATABASE_URL para conectar la base de datos Neon.")
        store = TxokoStore()
        request.app.state.txoko_store = store
    return store


@app.middleware("http")
async def security_headers(request: Request, call_next: Any) -> Response:
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "public, max-age=300"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; "
        "script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; "
        "base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
    )
    return response


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, error: ApiError) -> JSONResponse:
    return JSONResponse({"error": str(error)}, status_code=error.status)


@app.exception_handler(Exception)
async def unexpected_error_handler(_: Request, error: Exception) -> JSONResponse:
    LOG.exception("Unhandled Vercel API error", exc_info=error)
    return JSONResponse({"error": "Error interno del servidor."}, status_code=500)


@app.get("/")
@app.get("/index.html")
async def home() -> FileResponse:
    return FileResponse(ROOT / "index.html", media_type="text/html; charset=utf-8")


@app.get("/styles.css")
async def styles() -> FileResponse:
    return FileResponse(ROOT / "styles.css", media_type="text/css; charset=utf-8")


@app.get("/app.js")
async def javascript() -> FileResponse:
    return FileResponse(ROOT / "app.js", media_type="text/javascript; charset=utf-8")


async def payload_from(request: Request) -> dict[str, Any]:
    body = await request.body()
    if len(body) > MAX_REQUEST_BYTES:
        raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "La solicitud es demasiado grande.")
    try:
        payload = json.loads(body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ApiError(HTTPStatus.BAD_REQUEST, "El contenido JSON no es válido.") from error
    if not isinstance(payload, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "El contenido de la solicitud no es válido.")
    return payload


def session_token(request: Request) -> str | None:
    return request.cookies.get(SESSION_COOKIE)


def session_user(store: TxokoStore, request: Request, required: bool = False) -> Any:
    user = store.get_session_user(session_token(request))
    if required and user is None:
        raise ApiError(HTTPStatus.UNAUTHORIZED, "Inicia sesión para continuar.")
    return user


def enforce_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin:
        return
    host = request.headers.get("host", "")
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme).split(",", 1)[0].strip()
    parsed = urllib.parse.urlparse(origin)
    if (
        not parsed.netloc
        or parsed.netloc.casefold() != host.casefold()
        or parsed.scheme.casefold() != scheme.casefold()
    ):
        raise ApiError(HTTPStatus.FORBIDDEN, "Origen de solicitud no permitido.")


def json_response(
    content: Any,
    status_code: int = HTTPStatus.OK,
    token: str | None = None,
    clear_token: bool = False,
) -> JSONResponse:
    response = JSONResponse(content, status_code=status_code)
    if token is not None:
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            secure=bool(os.environ.get("VERCEL")),
            samesite="strict",
            path="/",
        )
    elif clear_token:
        response.delete_cookie(
            SESSION_COOKIE,
            httponly=True,
            secure=bool(os.environ.get("VERCEL")),
            samesite="strict",
            path="/",
        )
    return response


@app.api_route("/api/{api_path:path}", methods=["GET", "POST"])
async def api_router(request: Request, api_path: str) -> JSONResponse:
    store = store_for(request)
    method = request.method
    path = "/" + api_path
    if method == "GET":
        if path == "/status":
            return json_response({"needsPresident": store.needs_initial_president()})
        if path == "/me":
            user = session_user(store, request)
            return json_response({"user": public_user(user) if user else None})
        if path == "/products":
            return json_response({"products": store.list_products()})
        if path == "/inventory-events":
            return json_response({
                "events": store.list_inventory_events(session_user(store, request, required=True))
            })
        if path == "/ledger":
            with store.connect() as connection:
                return json_response(money_summary(connection))
        if path == "/debts":
            return json_response({"debts": store.list_debts(session_user(store, request))})
        if path == "/guests":
            return json_response({"guests": store.list_guests()})
        if path == "/guest-debts":
            guest_id = request.query_params.get("guestId", "")
            return json_response({"debts": store.guest_debts(guest_id)})
        if path == "/members":
            return json_response({"members": store.list_members(session_user(store, request, required=True))})
        if path == "/expenses":
            return json_response({"expenses": store.list_expenses(session_user(store, request, required=True))})
        raise ApiError(HTTPStatus.NOT_FOUND, "Ruta no encontrada.")

    enforce_same_origin(request)
    payload = await payload_from(request)
    if path == "/register":
        user, token = store.register(payload)
        return json_response({"user": public_user(user)}, HTTPStatus.CREATED, token=token)
    if path == "/login":
        remote = request.headers.get("x-forwarded-for", request.client.host if request.client else "")
        user, token = store.authenticate(remote.split(",", 1)[0].strip(), payload.get("username"), payload.get("pin"))
        return json_response({"user": public_user(user)}, token=token)
    if path == "/logout":
        store.logout(session_token(request))
        return json_response({"ok": True}, clear_token=True)
    if path == "/products":
        product = store.create_product(session_user(store, request, required=True), payload)
        return json_response({"product": product}, HTTPStatus.CREATED)
    if path == "/consume":
        result = store.consume(
            session_user(store, request),
            payload.get("productId", ""),
            payload.get("guestId"),
            payload.get("guestName"),
        )
        return json_response(result, HTTPStatus.CREATED)
    if path == "/stock":
        store.adjust_stock(session_user(store, request, required=True), payload.get("productId", ""), payload.get("amount"))
        return json_response({"ok": True})
    if path == "/archive-product":
        store.archive_product(session_user(store, request, required=True), payload.get("productId", ""))
        return json_response({"ok": True})
    if path == "/request-payment":
        store.request_payment(
            session_user(store, request),
            payload.get("transactionId", ""),
            payload.get("guestId"),
        )
        return json_response({"ok": True})
    if path == "/confirm-payment":
        store.confirm_payment(
            session_user(store, request, required=True),
            payload.get("transactionId", ""),
        )
        return json_response({"ok": True})
    if path == "/deactivate-member":
        store.deactivate_member(session_user(store, request, required=True), payload.get("userId", ""))
        return json_response({"ok": True})
    if path == "/expenses":
        expense = store.create_expense(session_user(store, request, required=True), payload)
        return json_response({"expense": expense}, HTTPStatus.CREATED)
    if path == "/approve-expense":
        store.review_expense(
            session_user(store, request, required=True),
            payload.get("expenseId"),
            approved=True,
        )
        return json_response({"ok": True})
    if path == "/reject-expense":
        store.review_expense(
            session_user(store, request, required=True),
            payload.get("expenseId"),
            approved=False,
        )
        return json_response({"ok": True})
    raise ApiError(HTTPStatus.NOT_FOUND, "Ruta no encontrada.")
