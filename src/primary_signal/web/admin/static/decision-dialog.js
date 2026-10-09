// Keep the ordinary details forms available until native dialogs are ready.
if (typeof HTMLDialogElement !== "undefined" &&
    typeof HTMLDialogElement.prototype.showModal === "function") {
  const openers = document.querySelectorAll("[data-decision-dialog]");
  let ready = openers.length > 0;

  for (const opener of openers) {
    const dialog = document.getElementById(opener.dataset.decisionDialog);
    const cancel = dialog?.querySelector(".ps-desk-dialog-cancel");
    const reason = dialog?.querySelector("textarea[name=reason]");
    if (!dialog || !cancel || !reason) {
      ready = false;
      break;
    }
    opener.addEventListener("click", () => {
      dialog.showModal();
      reason.focus();
    });
    cancel.addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => {
      dialog.querySelector("form")?.reset();
      opener.focus();
    });
  }

  if (ready) {
    for (const opener of openers) {
      opener.hidden = false;
      opener.parentElement.querySelector(".ps-desk-decision-fallback").hidden = true;
    }
  }
}
