const $ = (selector) => document.querySelector(selector);

const authPanel = $("#auth-panel");
const authNotice = $("#auth-notice");
const appContent = $("#app-content");
const accountLabel = $("#account-label");
const signInButton = $("#sign-in-button");
const signOutButton = $("#sign-out-button");
const notice = $("#notice");
const productList = $("#product-list");
const productsEmpty = $("#products-empty");
const debtList = $("#debt-list");
const debtsEmpty = $("#debts-empty");
const consumeDialog = $("#consume-dialog");
const guestConsumerForm = $("#guest-consumer-form");
const newGuestFields = $("#new-guest-fields");
const dialogError = $("#dialog-error");

let currentUser = null;
let activeProduct = null;
let isNewGuest = false;
let noticeTimer;
let refreshTimer;
let products = [];
let mediaItems = [];
let activeFilter = "all";
let objectUrls = [];

async function api(path, { method = "GET", body } = {}) {
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let result;
  try {
    result = await response.json();
  } catch (error) {
    throw new Error(`El servidor respondió con un formato inesperado (${response.status}).`, { cause: error });
  }
  if (!response.ok) throw new Error(result.error || `Error del servidor (${response.status}).`);
  return result;
}

async function call(operation, data = {}) {
  switch (operation) {
    case "listGuests":
      return { data: await api("/api/guests") };
    case "getGuestDebts":
      return { data: await api(`/api/guest-debts?guestId=${encodeURIComponent(data.guestId)}`) };
    case "createProduct":
      return { data: await api("/api/products", { method: "POST", body: data }) };
    case "consumeProduct":
      return { data: await api("/api/consume", { method: "POST", body: data }) };
    case "adjustStock":
      return { data: await api("/api/stock", { method: "POST", body: data }) };
    case "archiveProduct":
      return { data: await api("/api/archive-product", { method: "POST", body: data }) };
    case "requestPayment":
      return { data: await api("/api/request-payment", { method: "POST", body: data }) };
    case "confirmPayment":
      return { data: await api("/api/confirm-payment", { method: "POST", body: data }) };
    case "removeMember":
      return { data: await api("/api/deactivate-member", { method: "POST", body: data }) };
    default:
      throw new Error(`Operación desconocida: ${operation}`);
  }
}

function displayError(error) {
  return error?.message || "Ha ocurrido un error inesperado.";
}

function showNotice(message, isError = false) {
  window.clearTimeout(noticeTimer);
  notice.textContent = message;
  notice.classList.toggle("is-error", isError);
  if (message) {
    noticeTimer = window.setTimeout(() => {
      notice.textContent = "";
      notice.classList.remove("is-error");
    }, 6000);
  }
}

function showAuthNotice(message, isError = false) {
  authNotice.textContent = message;
  authNotice.classList.toggle("is-error", isError);
}

function formatMoney(cents) {
  return new Intl.NumberFormat("es-ES", { style: "currency", currency: "EUR" })
    .format((cents || 0) / 100);
}

function safeText(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  element.textContent = text;
  return element;
}

function showTab(tabName) {
  document.querySelectorAll(".tab-button").forEach((button) => {
    const selected = button.dataset.tab === tabName;
    button.classList.toggle("is-active", selected);
    button.setAttribute("aria-selected", String(selected));
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.hidden = panel.id !== `${tabName}-section`;
  });
}

function setUser(user) {
  currentUser = user;
  authPanel.hidden = Boolean(user);
  accountLabel.textContent = user ? `${user.firstName} ${user.lastName}` : "Modo invitado";
  signInButton.hidden = Boolean(user);
  signOutButton.hidden = !user;
  $("#role-badge").hidden = user?.role !== "president";
  $("#members-tab").hidden = user?.role !== "president";
  $("#product-form").hidden = user?.role !== "president";
  $("#guest-debt-form").hidden = Boolean(user);
  $("#expense-form").hidden = !user || user.role === "former_member";
  $("#expenses-login-hint").hidden = Boolean(user);
  if (!user) {
    $("#expenses-list").replaceChildren();
    $("#expenses-empty").hidden = true;
  }
  appContent.hidden = false;
  refreshData();
}

async function refreshData() {
  try {
    const [inventory, ledger, debtResult] = await Promise.all([
      api("/api/products"),
      api("/api/ledger"),
      api("/api/debts"),
    ]);
    products = inventory.products;
    renderProducts();
    $("#funds-total").textContent = formatMoney(ledger.totalSalesCents);
    $("#funds-detail").textContent = `Pendiente: ${formatMoney(ledger.pendingCents)} · Cobrado: ${formatMoney(ledger.collectedCents)}`;
    $("#pot-balance").textContent = formatMoney(ledger.potBalanceCents);
    $("#pot-detail").textContent = `Ingresos confirmados: ${formatMoney(ledger.collectedCents)} · Gastos aprobados: ${formatMoney(ledger.approvedExpensesCents)}`;
    renderDebts(debtResult.debts);
    if (currentUser && !$("#pot-section").hidden) await loadExpenses();
    if (currentUser?.role === "president" && !$("#members-section").hidden) await loadMembers();
  } catch (error) {
    showNotice(`No se pudieron actualizar los datos: ${displayError(error)}`, true);
  }
}

function renderProducts() {
  productList.replaceChildren();
  productsEmpty.hidden = products.length > 0;

  products.forEach((product) => {
    const card = document.createElement("article");
    card.className = "product-card";
    const info = document.createElement("div");
    info.className = "product-info";
    info.append(
      safeText("h3", "product-name", product.name),
      safeText("p", "product-price", `${formatMoney(product.priceCents)} · ${product.stock} disponibles`),
    );

    const actions = document.createElement("div");
    actions.className = "product-actions";
    const consumeButton = safeText("button", "button button-consume", "− Consumir");
    consumeButton.type = "button";
    consumeButton.disabled = product.stock < 1;
    consumeButton.setAttribute("aria-label", `Consumir una unidad de ${product.name}`);
    consumeButton.addEventListener("click", () => beginConsumption(product));
    actions.append(consumeButton);

    if (currentUser?.role === "president") {
      const stockButton = safeText("button", "button button-quiet", "+ Stock");
      stockButton.type = "button";
      stockButton.addEventListener("click", () => changeStock(product));
      const archiveButton = safeText("button", "button button-quiet", "Quitar");
      archiveButton.type = "button";
      archiveButton.addEventListener("click", () => archiveProduct(product));
      actions.append(stockButton, archiveButton);
    }
    card.append(info, actions);
    productList.append(card);
  });
}

function renderDebts(debts) {
  debtList.replaceChildren();
  debtsEmpty.hidden = debts.length > 0;
  debts.forEach((debt) => debtList.append(createDebtCard(debt)));
}

function createDebtCard(debt, guest = false) {
  const card = document.createElement("article");
  card.className = "debt-card";
  const info = document.createElement("div");
  info.className = "debt-info";
  const byline = debt.consumer?.name
    ? `${debt.consumer.name} ${debt.consumer.lastName}`
    : (debt.itemName || "Consumo");
  info.append(
    safeText("h3", "", guest ? debt.itemName : byline),
    safeText("p", "debt-description", guest
      ? `${byline} · ${debt.itemName}`
      : `${debt.itemName} · ${formatMoney(debt.amountCents)}`),
  );
  const actions = document.createElement("div");
  actions.className = "debt-actions";
  actions.append(safeText("strong", "debt-amount", formatMoney(debt.amountCents)));

  if (debt.status === "payment_requested" && currentUser?.role === "president") {
    const confirm = safeText("button", "button button-primary", "Confirmar pago");
    confirm.type = "button";
    confirm.addEventListener("click", () => confirmPayment(debt.id));
    actions.append(confirm);
  } else if (debt.status === "payment_requested") {
    actions.append(safeText("span", "status-badge is-pending", "Pendiente de confirmar"));
  } else if (currentUser?.role === "president") {
    actions.append(safeText("span", "status-badge is-pending", "Pendiente de pago"));
  } else {
    const paid = safeText("button", "button button-secondary", "He pagado");
    paid.type = "button";
    paid.addEventListener("click", () => requestPayment(debt));
    actions.append(paid);
  }
  card.append(info, actions);
  return card;
}

async function loadExpenses() {
  try {
    const result = await api("/api/expenses");
    renderExpenses(result.expenses);
  } catch (error) {
    showNotice(`No se pudieron cargar las compras: ${displayError(error)}`, true);
  }
}

function renderExpenses(expenses) {
  const list = $("#expenses-list");
  list.replaceChildren();
  $("#expenses-empty").hidden = expenses.length > 0;
  expenses.forEach((expense) => {
    const card = document.createElement("article");
    card.className = "expense-card";
    const info = document.createElement("div");
    info.className = "expense-info";
    const created = new Date(expense.createdAt * 1000).toLocaleString("es-ES");
    info.append(
      safeText("h3", "", expense.description),
      safeText("p", "debt-description", `${formatMoney(expense.amountCents)} · Solicitado por ${expense.requesterName} · ${created}`),
    );
    const actions = document.createElement("div");
    actions.className = "expense-actions";
    actions.append(safeText("strong", "debt-amount", formatMoney(expense.amountCents)));
    if (expense.status === "pending") {
      if (currentUser?.role === "president") {
        const approve = safeText("button", "button button-primary", "Aprobar");
        approve.type = "button";
        approve.addEventListener("click", () => reviewExpense(expense, true));
        const reject = safeText("button", "button button-danger", "Rechazar");
        reject.type = "button";
        reject.addEventListener("click", () => reviewExpense(expense, false));
        actions.append(approve, reject);
      } else {
        actions.append(safeText("span", "status-badge is-pending", "Pendiente de revisión"));
      }
    } else {
      const status = expense.status === "approved" ? "Aprobado" : "Rechazado";
      actions.append(safeText(
        "span",
        `status-badge ${expense.status === "approved" ? "is-approved" : "is-rejected"}`,
        status,
      ));
      if (expense.reviewerName) {
        info.append(safeText("p", "expense-review", `${status} por ${expense.reviewerName} · ${new Date(expense.reviewedAt * 1000).toLocaleString("es-ES")}`));
      }
    }
    card.append(info, actions);
    list.append(card);
  });
}

async function reviewExpense(expense, approved) {
  const action = approved ? "aprobar" : "rechazar";
  if (!window.confirm(`¿Quieres ${action} "${expense.description}" por ${formatMoney(expense.amountCents)}?`)) return;
  try {
    await api(approved ? "/api/approve-expense" : "/api/reject-expense", {
      method: "POST",
      body: { expenseId: expense.id },
    });
    showNotice(approved ? "Compra aprobada y descontada del bote." : "Compra rechazada.");
    await refreshData();
  } catch (error) {
    showNotice(`No se pudo ${action} la compra: ${displayError(error)}`, true);
  }
}

async function loadMembers() {
  const memberList = $("#member-list");
  memberList.replaceChildren();
  try {
    const result = await api("/api/members");
    result.members.forEach((member) => {
      const row = document.createElement("article");
      row.className = "member-row";
      const details = document.createElement("div");
      details.append(
        safeText("strong", "", `${member.firstName} ${member.lastName}`),
        safeText("span", "member-email", `@${member.username}`),
      );
      const remove = safeText("button", "button button-danger", "Retirar acceso");
      remove.type = "button";
      remove.addEventListener("click", async () => {
        if (!window.confirm(`¿Retirar el acceso de ${member.firstName} ${member.lastName}? Se conservarán sus deudas e historial.`)) return;
        remove.disabled = true;
        try {
          await call("removeMember", { userId: member.id });
          showNotice("Acceso retirado. Su historial financiero se conserva.");
          await loadMembers();
        } catch (error) {
          remove.disabled = false;
          showNotice(`No se pudo retirar el acceso: ${displayError(error)}`, true);
        }
      });
      row.append(details, remove);
      memberList.append(row);
    });
    if (!memberList.childElementCount) memberList.append(safeText("p", "muted-copy", "No hay otras cuentas activas."));
  } catch (error) {
    showNotice(`No se pudieron cargar los miembros: ${displayError(error)}`, true);
  }
}

async function beginConsumption(product) {
  if (product.stock < 1) return;
  activeProduct = product;
  $("#consume-title").textContent = `Consumir ${product.name}`;
  $("#consume-description").textContent = `Queda(n) ${product.stock}. Precio: ${formatMoney(product.priceCents)}.`;
  dialogError.textContent = "";
  guestConsumerForm.reset();
  newGuestFields.hidden = true;
  isNewGuest = false;
  $("#toggle-new-guest").textContent = "Es la primera vez que vengo";
  const isGuest = !currentUser;
  $("#member-consumer").hidden = isGuest;
  guestConsumerForm.hidden = !isGuest;
  if (isGuest) await loadGuestOptions();
  consumeDialog.showModal();
}

async function loadGuestOptions() {
  try {
    const result = await call("listGuests");
    document.querySelectorAll('select[name="guestId"]').forEach((select) => {
      const initialText = select.options[0]?.textContent || "Selecciona tu nombre";
      select.replaceChildren(new Option(initialText, ""));
      result.data.guests.forEach((guest) => select.add(new Option(`${guest.firstName} ${guest.lastName}`, guest.id)));
    });
  } catch (error) {
    dialogError.textContent = `No se pudo cargar la lista de invitados: ${displayError(error)}`;
  }
}

async function consume(product, guestId = null, guestName = null) {
  try {
    await call("consumeProduct", {
      productId: product.id,
      ...(guestId ? { guestId } : {}),
      ...(guestName ? { guestName } : {}),
    });
    consumeDialog.close();
    showNotice(`${product.name} añadido a la deuda. Los fondos se actualizarán.`);
    await refreshData();
  } catch (error) {
    dialogError.textContent = displayError(error);
    showNotice(`No se pudo registrar el consumo: ${displayError(error)}`, true);
  }
}

async function changeStock(product) {
  const answer = window.prompt(`¿Cuántas unidades de ${product.name} quieres añadir?`, "1");
  if (answer === null) return;
  const amount = Number(answer);
  if (!Number.isSafeInteger(amount) || amount < 1) {
    showNotice("Introduce una cantidad entera mayor que cero.", true);
    return;
  }
  try {
    await call("adjustStock", { productId: product.id, amount });
    showNotice(`Añadidas ${amount} unidades de ${product.name}.`);
    await refreshData();
  } catch (error) {
    showNotice(`No se pudo actualizar el stock: ${displayError(error)}`, true);
  }
}

async function archiveProduct(product) {
  if (!window.confirm(`¿Quitar ${product.name} del inventario? El historial de consumos se conserva.`)) return;
  try {
    await call("archiveProduct", { productId: product.id });
    showNotice(`${product.name} retirado del inventario.`);
    await refreshData();
  } catch (error) {
    showNotice(`No se pudo quitar el producto: ${displayError(error)}`, true);
  }
}

async function requestPayment(debt) {
  if (!window.confirm("¿Confirmas que has pagado esta deuda? Quedará pendiente de aprobación del presidente.")) return;
  try {
    await call("requestPayment", {
      transactionId: debt.id,
      ...(currentUser ? {} : { guestId: debt.consumer?.guestId }),
    });
    showNotice("Aviso enviado. El presidente debe confirmar el pago.");
    await refreshData();
  } catch (error) {
    showNotice(`No se pudo avisar del pago: ${displayError(error)}`, true);
  }
}

async function confirmPayment(transactionId) {
  if (!window.confirm("¿Confirmas que has recibido este pago?")) return;
  try {
    await call("confirmPayment", { transactionId });
    showNotice("Pago confirmado.");
    await refreshData();
  } catch (error) {
    showNotice(`No se pudo confirmar el pago: ${displayError(error)}`, true);
  }
}

async function loadGuestDebts(guestId) {
  try {
    const result = await call("getGuestDebts", { guestId });
    const debts = result.data.debts || [];
    debtList.replaceChildren();
    debtsEmpty.hidden = debts.length > 0;
    debts.forEach((debt) => debtList.append(createDebtCard(debt, true)));
    if (!debts.length) debtsEmpty.querySelector("h3").textContent = "No hay deudas pendientes";
    showTab("debts");
  } catch (error) {
    showNotice(`No se pudieron consultar las deudas: ${displayError(error)}`, true);
  }
}

function centsFromPrice(price) {
  const value = Number(price);
  if (!Number.isFinite(value) || value <= 0) return null;
  const cents = Math.round(value * 100);
  return Number.isSafeInteger(cents) && cents > 0 ? cents : null;
}

function setupEvents() {
  signInButton.addEventListener("click", () => {
    authPanel.hidden = !authPanel.hidden;
    if (!authPanel.hidden) authPanel.scrollIntoView({ behavior: "smooth", block: "center" });
  });

  $("#login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    const button = formElement.querySelector('button[type="submit"]');
    button.disabled = true;
    showAuthNotice("");
    try {
      const result = await api("/api/login", {
        method: "POST",
        body: { username: form.get("username"), pin: form.get("pin") },
      });
      formElement.reset();
      setUser(result.user);
      showNotice(`Bienvenido, ${result.user.firstName}.`);
    } catch (error) {
      showAuthNotice(displayError(error), true);
    } finally {
      button.disabled = false;
    }
  });

  $("#register-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    const button = formElement.querySelector('button[type="submit"]');
    button.disabled = true;
    showAuthNotice("");
    try {
      const result = await api("/api/register", {
        method: "POST",
        body: {
          firstName: form.get("firstName"),
          lastName: form.get("lastName"),
          username: form.get("username"),
          pin: form.get("pin"),
          setupToken: form.get("setupToken"),
        },
      });
      formElement.reset();
      setUser(result.user);
      showNotice(result.user.role === "president"
        ? "Cuenta creada. Eres el presidente de este Txoko."
        : `Cuenta creada. Bienvenido, ${result.user.firstName}.`);
    } catch (error) {
      showAuthNotice(displayError(error), true);
    } finally {
      button.disabled = false;
    }
  });

  signOutButton.addEventListener("click", async () => {
    try {
      await api("/api/logout", { method: "POST", body: {} });
      setUser(null);
      showNotice("Has cerrado sesión.");
    } catch (error) {
      showNotice(`No se pudo cerrar sesión: ${displayError(error)}`, true);
    }
  });

  $("#product-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    const name = String(form.get("name")).trim();
    const quantity = Number(form.get("quantity"));
    const priceCents = centsFromPrice(form.get("price"));
    if (!name || !Number.isSafeInteger(quantity) || quantity < 0 || priceCents === null) {
      showNotice("Comprueba el nombre, la cantidad y el precio del producto.", true);
      return;
    }
    const button = formElement.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
      await call("createProduct", { name, stock: quantity, priceCents });
      formElement.reset();
      formElement.elements.quantity.value = "1";
      showNotice(`${name} añadido al inventario.`);
      await refreshData();
    } catch (error) {
      showNotice(`No se pudo añadir el producto: ${displayError(error)}`, true);
    } finally {
      button.disabled = false;
    }
  });

  $("#expense-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    const amountCents = centsFromPrice(form.get("amount"));
    const description = String(form.get("description")).trim();
    if (!description || amountCents === null) {
      showNotice("Comprueba la descripción y el importe de la compra.", true);
      return;
    }
    const button = formElement.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
      await api("/api/expenses", {
        method: "POST",
        body: { description, amountCents },
      });
      formElement.reset();
      showNotice("Compra enviada al presidente para su revisión.");
      await refreshData();
    } catch (error) {
      showNotice(`No se pudo registrar la compra: ${displayError(error)}`, true);
    } finally {
      button.disabled = false;
    }
  });

  document.querySelectorAll(".tab-button").forEach((button) => {
    button.addEventListener("click", () => {
      showTab(button.dataset.tab);
      if (button.dataset.tab === "members" && currentUser?.role === "president") loadMembers();
      if (button.dataset.tab === "pot" && currentUser) loadExpenses();
    });
  });

  $("#member-consume-button").addEventListener("click", () => {
    if (activeProduct) consume(activeProduct);
  });

  $("#toggle-new-guest").addEventListener("click", () => {
    isNewGuest = !isNewGuest;
    newGuestFields.hidden = !isNewGuest;
    const guestSelect = guestConsumerForm.elements.guestId;
    guestSelect.required = !isNewGuest;
    guestSelect.disabled = isNewGuest;
    newGuestFields.querySelectorAll("input").forEach((input) => { input.required = isNewGuest; });
    $("#toggle-new-guest").textContent = isNewGuest ? "Ya he venido antes" : "Es la primera vez que vengo";
  });

  guestConsumerForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!activeProduct) return;
    if (isNewGuest) {
      const firstName = guestConsumerForm.elements.firstName.value.trim();
      const lastName = guestConsumerForm.elements.lastName.value.trim();
      if (firstName && lastName) await consume(activeProduct, null, { firstName, lastName });
    } else {
      const guestId = guestConsumerForm.elements.guestId.value;
      if (guestId) await consume(activeProduct, guestId);
    }
  });

  $("#guest-debt-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const guestId = event.currentTarget.elements.guestId.value;
    if (guestId) loadGuestDebts(guestId);
  });

  consumeDialog.addEventListener("click", (event) => {
    if (event.target === consumeDialog) consumeDialog.close();
  });
  setupMediaEvents();
}

async function initializeApp() {
  appContent.hidden = true;
  authPanel.hidden = true;
  accountLabel.textContent = "Conectando al servidor…";
  try {
    const [status, session] = await Promise.all([api("/api/status"), api("/api/me")]);
    $("#setup-token-field").hidden = !status.needsPresident;
    $("#setup-token-field").querySelector("input").required = status.needsPresident;
    setUser(session.user);
    refreshTimer = window.setInterval(async () => {
      try {
        const result = await api("/api/me");
        if (result.user?.id !== currentUser?.id) setUser(result.user);
        else if (currentUser) await refreshData();
      } catch (error) {
        showNotice(`Se perdió la conexión con el servidor: ${displayError(error)}`, true);
      }
    }, 10_000);
  } catch (error) {
    accountLabel.textContent = "Servidor no disponible";
    authPanel.hidden = false;
    $("#auth-panel > h1").textContent = "Inicia el servidor de Txoko";
    $("#auth-panel > p").textContent = "En Termux, ejecuta «python server.py» y vuelve a cargar esta página.";
    $("#auth-forms").hidden = true;
    showAuthNotice(displayError(error), true);
  }
}

const MEDIA_DATABASE_NAME = "txoko-media";
const MEDIA_STORE_NAME = "media";
const fileInput = $("#file-input");
const dropZone = $("#drop-zone");
const mediaGrid = $("#media-grid");
const mediaEmptyState = $("#media-empty-state");
const itemCount = $("#item-count");
const searchInput = $("#search-input");
const clearButton = $("#clear-button");
const filterButtons = [...document.querySelectorAll(".filter-button")];

function openMediaDatabase() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(MEDIA_DATABASE_NAME, 1);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(MEDIA_STORE_NAME)) {
        request.result.createObjectStore(MEDIA_STORE_NAME, { keyPath: "id", autoIncrement: true });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("No se pudo abrir la biblioteca multimedia."));
  });
}

const mediaDatabasePromise = openMediaDatabase();

function mediaStore(mode, operation) {
  return mediaDatabasePromise.then((database) => new Promise((resolve, reject) => {
    const transaction = database.transaction(MEDIA_STORE_NAME, mode);
    const request = operation(transaction.objectStore(MEDIA_STORE_NAME));
    transaction.oncomplete = () => resolve(request?.result);
    transaction.onerror = () => reject(transaction.error ?? new Error("No se pudo completar la operación."));
    transaction.onabort = () => reject(transaction.error ?? new Error("La operación fue cancelada."));
  }));
}

function getMediaType(type) {
  if (type.startsWith("image/")) return "image";
  if (type.startsWith("video/")) return "video";
  if (type.startsWith("audio/")) return "audio";
  return null;
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function renderMediaLibrary() {
  objectUrls.forEach((url) => URL.revokeObjectURL(url));
  objectUrls = [];
  mediaGrid.replaceChildren();
  const search = searchInput.value.trim().toLocaleLowerCase();
  const visibleItems = mediaItems.filter((item) =>
    (activeFilter === "all" || item.kind === activeFilter) && item.name.toLocaleLowerCase().includes(search));

  visibleItems.forEach((item) => {
    const card = document.createElement("article");
    card.className = "media-card";
    const preview = document.createElement("div");
    preview.className = "media-preview";
    const url = URL.createObjectURL(item.blob);
    objectUrls.push(url);
    if (item.kind === "image") {
      const image = document.createElement("img");
      image.src = url;
      image.alt = item.name;
      preview.append(image);
    } else {
      const media = document.createElement(item.kind === "video" ? "video" : "audio");
      media.src = url;
      media.controls = true;
      media.preload = "metadata";
      if (item.kind === "video") media.playsInline = true;
      preview.append(media);
    }
    const info = document.createElement("div");
    info.className = "media-info";
    const name = safeText("h3", "media-name", item.name);
    const remove = safeText("button", "delete-button", "Eliminar");
    remove.type = "button";
    remove.addEventListener("click", async () => {
      if (!window.confirm(`¿Eliminar "${item.name}" de esta biblioteca?`)) return;
      try {
        await mediaStore("readwrite", (store) => store.delete(item.id));
        await loadMediaLibrary();
      } catch (error) {
        showNotice(`No se pudo eliminar el archivo: ${displayError(error)}`, true);
      }
    });
    const row = document.createElement("div");
    row.className = "media-title-row";
    row.append(name, remove);
    info.append(row, safeText("p", "media-meta", `${item.kind === "image" ? "Imagen" : item.kind === "video" ? "Vídeo" : "Audio"} · ${formatSize(item.size)}`));
    card.append(preview, info);
    mediaGrid.append(card);
  });
  itemCount.textContent = String(mediaItems.length);
  clearButton.hidden = mediaItems.length === 0;
  mediaEmptyState.hidden = visibleItems.length > 0;
  if (mediaItems.length && !visibleItems.length) {
    mediaEmptyState.querySelector("h3").textContent = "No hay resultados";
    mediaEmptyState.querySelector("p").textContent = "Prueba otra búsqueda o cambia el filtro.";
  } else {
    mediaEmptyState.querySelector("h3").textContent = "Tu biblioteca está esperando";
    mediaEmptyState.querySelector("p").textContent = "Añade un archivo para empezar a crear tu colección.";
  }
}

async function loadMediaLibrary() {
  try {
    mediaItems = (await mediaStore("readonly", (store) => store.getAll()))
      .sort((first, second) => second.addedAt - first.addedAt);
    renderMediaLibrary();
  } catch (error) {
    showNotice(`No se pudo cargar la biblioteca multimedia: ${displayError(error)}`, true);
  }
}

async function addMediaFiles(files) {
  const supported = [...files].filter((file) => getMediaType(file.type));
  if (!supported.length) {
    if (files.length) showNotice("Selecciona archivos de imagen, vídeo o audio.", true);
    return;
  }
  try {
    const database = await mediaDatabasePromise;
    await new Promise((resolve, reject) => {
      const transaction = database.transaction(MEDIA_STORE_NAME, "readwrite");
      supported.forEach((file) => transaction.objectStore(MEDIA_STORE_NAME).add({
        name: file.name,
        type: file.type,
        kind: getMediaType(file.type),
        size: file.size,
        addedAt: Date.now(),
        blob: file,
      }));
      transaction.oncomplete = resolve;
      transaction.onerror = () => reject(transaction.error ?? new Error("No se pudieron guardar los archivos."));
    });
    await loadMediaLibrary();
    showNotice(`${supported.length} archivo(s) añadido(s) a la biblioteca local.`);
  } catch (error) {
    showNotice(`No se pudieron guardar los archivos: ${displayError(error)}`, true);
  }
}

function setupMediaEvents() {
  fileInput.addEventListener("change", async () => {
    await addMediaFiles(fileInput.files);
    fileInput.value = "";
  });
  searchInput.addEventListener("input", renderMediaLibrary);
  filterButtons.forEach((button) => button.addEventListener("click", () => {
    activeFilter = button.dataset.filter;
    filterButtons.forEach((filter) => {
      const selected = filter === button;
      filter.classList.toggle("is-active", selected);
      filter.setAttribute("aria-pressed", String(selected));
    });
    renderMediaLibrary();
  }));
  clearButton.addEventListener("click", async () => {
    if (!window.confirm("¿Eliminar todos los archivos de esta biblioteca local?")) return;
    try {
      await mediaStore("readwrite", (store) => store.clear());
      await loadMediaLibrary();
    } catch (error) {
      showNotice(`No se pudo vaciar la biblioteca: ${displayError(error)}`, true);
    }
  });
  ["dragenter", "dragover"].forEach((name) => dropZone.addEventListener(name, (event) => {
    event.preventDefault();
    dropZone.classList.add("is-dragging");
  }));
  ["dragleave", "drop"].forEach((name) => dropZone.addEventListener(name, (event) => {
    event.preventDefault();
    dropZone.classList.remove("is-dragging");
  }));
  dropZone.addEventListener("drop", (event) => addMediaFiles(event.dataTransfer.files));
}

setupEvents();
loadMediaLibrary();
initializeApp();
