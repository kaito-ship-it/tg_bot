// ==UserScript==
// @name         Nedra.kz Autofill News
// @namespace    https://nedra.kz/
// @version      1.3.1
// @description  Opens the create modal, fills a prepared Telegram draft, and creates the publication after validation.
// @match        https://dev.nedra.kz/admin/news*
// @grant        GM_xmlhttpRequest
// @connect      localhost
// @connect      127.0.0.1
// ==/UserScript==

(function () {
  'use strict';

  const DRAFT_API_BASE = 'http://localhost:8000';
  const WAIT_TIMEOUT_MS = 12000;
  const UPLOAD_TIMEOUT_MS = 45000;
  const SUBMIT_TIMEOUT_MS = 30000;
  const DEFAULT_CATEGORY_ID = 35;
  const DOCUMENTS_SCHEMA = 'mountedActionSchema0.documents';
  const MAX_DOCUMENT_ROWS = 20;

  function getParam(name) {
    return new URLSearchParams(window.location.search).get(name);
  }

  function waitFor(selector, timeout = WAIT_TIMEOUT_MS) {
    return new Promise((resolve, reject) => {
      const existing = document.querySelector(selector);
      if (existing) return resolve(existing);
      const timer = window.setTimeout(() => {
        observer.disconnect();
        reject(new Error(`Timeout waiting for ${selector}`));
      }, timeout);
      const observer = new MutationObserver(() => {
        const element = document.querySelector(selector);
        if (!element) return;
        window.clearTimeout(timer);
        observer.disconnect();
        resolve(element);
      });
      observer.observe(document.documentElement, { childList: true, subtree: true });
    });
  }

  function delay(milliseconds) {
    return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
  }

  function waitUntil(predicate, timeout = WAIT_TIMEOUT_MS, description = 'condition') {
    return new Promise((resolve, reject) => {
      const startedAt = Date.now();
      const check = () => {
        const result = predicate();
        if (result) return resolve(result);
        if (Date.now() - startedAt >= timeout) {
          return reject(new Error(`Timeout waiting for ${description}`));
        }
        window.setTimeout(check, 80);
      };
      check();
    });
  }

  function findDocumentDeleteButtons() {
    return Array.from(document.querySelectorAll('button[wire\\:click]')).filter(
      (button) => {
        const action = button.getAttribute('wire:click') || '';
        return action.includes("mountAction('delete'") && action.includes(DOCUMENTS_SCHEMA);
      },
    );
  }

  function findDeleteConfirmationButton() {
    const dialogs = Array.from(
      document.querySelectorAll('[role="dialog"], .fi-modal-window'),
    ).filter((dialog) => dialog.getClientRects().length > 0);
    const dialog = dialogs.at(-1);
    if (!dialog) return null;
    return Array.from(dialog.querySelectorAll('button')).find((button) => {
      const label = (button.textContent || '').trim();
      return /^(Удалить|Подтвердить|Да, удалить)$/i.test(label);
    }) || null;
  }

  async function removeDocumentRows() {
    await delay(150);
    let removed = 0;
    while (removed < MAX_DOCUMENT_ROWS) {
      const buttons = findDocumentDeleteButtons();
      if (buttons.length === 0) return removed;
      const button = buttons[0];
      const countBefore = buttons.length;
      button.click();
      let confirmationClicked = false;
      await waitUntil(
        () => {
          if (!button.isConnected || findDocumentDeleteButtons().length < countBefore) {
            return true;
          }
          if (!confirmationClicked) {
            const confirmationButton = findDeleteConfirmationButton();
            if (confirmationButton && confirmationButton !== button) {
              confirmationButton.click();
              confirmationClicked = true;
            }
          }
          return false;
        },
        5000,
        'the documents section to update',
      );
      removed += 1;
    }
    if (findDocumentDeleteButtons().length > 0) {
      throw new Error(`Too many document rows (more than ${MAX_DOCUMENT_ROWS})`);
    }
    return removed;
  }

  function request(url, responseType) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: 'GET', url, responseType, timeout: WAIT_TIMEOUT_MS,
        onload(response) {
          if (response.status >= 200 && response.status < 300) resolve(response.response);
          else reject(new Error(`GET ${url} failed: ${response.status}`));
        },
        onerror: () => reject(new Error(`GET ${url} failed`)),
        ontimeout: () => reject(new Error(`GET ${url} timed out`)),
      });
    });
  }

  async function fetchDraft(draftId) {
    const result = await request(
      `${DRAFT_API_BASE}/draft/${encodeURIComponent(draftId)}`, 'json',
    );
    if (!result || typeof result !== 'object') {
      throw new Error('Draft API returned invalid JSON');
    }
    return result;
  }

  function postJson(url, payload) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: 'POST', url,
        headers: { 'Content-Type': 'application/json' },
        data: JSON.stringify(payload), responseType: 'json', timeout: WAIT_TIMEOUT_MS,
        onload(response) {
          if (response.status >= 200 && response.status < 300) resolve(response.response);
          else reject(new Error(`POST ${url} failed: ${response.status}`));
        },
        onerror: () => reject(new Error(`POST ${url} failed`)),
        ontimeout: () => reject(new Error(`POST ${url} timed out`)),
      });
    });
  }

  async function reportPublication(draftId, token, success, error = null) {
    if (!token) return;
    const url = `${DRAFT_API_BASE}/publication/${encodeURIComponent(draftId)}/result`;
    let lastError = null;
    for (let attempt = 1; attempt <= 3; attempt += 1) {
      try {
        await postJson(url, { token, success, error });
        return;
      } catch (requestError) {
        lastError = requestError;
        if (attempt < 3) await delay(500 * attempt);
      }
    }
    throw lastError;
  }

  function setNativeValue(element, value) {
    const descriptor = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(element), 'value');
    if (descriptor?.set) descriptor.set.call(element, value);
    else element.value = value;
    element.dispatchEvent(new Event('input', { bubbles: true }));
    element.dispatchEvent(new Event('change', { bubbles: true }));
  }

  async function setPhoto(fileUrl) {
    const blob = await request(fileUrl, 'blob');
    const type = blob.type || 'image/jpeg';
    const extension = type === 'image/png' ? 'png' : 'jpg';
    const file = new File([blob], `cover.${extension}`, { type });
    const input = await waitFor('input.filepond--browser[accept*="image/jpeg"]');
    const transfer = new DataTransfer();
    transfer.items.add(file);
    input.files = transfer.files;
    input.dispatchEvent(new Event('change', { bubbles: true }));
    const filePond = input.closest('.filepond--root') || input.parentElement;
    await waitUntil(
      () => filePond?.querySelector('.filepond--item')
        ?.getAttribute('data-filepond-item-state') === 'processing-complete',
      UPLOAD_TIMEOUT_MS,
      'the image upload to complete',
    );
  }

  async function fillEditor(editor, text) {
    editor.focus();
    document.execCommand('selectAll', false, null);
    if (!document.execCommand('insertText', false, text)) {
      throw new Error('Tiptap rejected insertText');
    }
    editor.dispatchEvent(new Event('input', { bubbles: true }));
  }

  function getCreateDialog() {
    const titleInput = document.querySelector('#mountedActionSchema0\\.title');
    if (!titleInput) return null;
    const closestDialog = titleInput.closest(
      '[role="dialog"], .fi-modal-window, .fi-modal',
    );
    if (closestDialog) return closestDialog;
    const visibleDialogs = Array.from(
      document.querySelectorAll('[role="dialog"], .fi-modal-window, .fi-modal'),
    ).filter((dialog) => dialog.getClientRects().length > 0);
    return visibleDialogs.find((dialog) => dialog.contains(titleInput))
      || titleInput.closest('form') || null;
  }

  function isButtonReady(button) {
    return Boolean(button && button.isConnected && !button.disabled
      && button.getAttribute('aria-disabled') !== 'true');
  }

  function findPublicationCreateButton({ requireReady = false } = {}) {
    const dialog = getCreateDialog();
    const scopes = dialog ? [dialog, document] : [document];
    for (const scope of scopes) {
      const buttons = Array.from(scope.querySelectorAll('button')).filter(
        (button) => button.getClientRects().length > 0,
      );
      const candidate = buttons.find((button) => {
        const label = (button.textContent || '').trim();
        const action = button.getAttribute('wire:click') || '';
        const isOuterOpenButton = action.includes("mountAction('create')");
        const isSubmitAction = button.type === 'submit' || action.includes('callMountedAction');
        return !isOuterOpenButton
          && (isSubmitAction || /^(Создать|Сохранить)( публикацию)?$/i.test(label))
          && (!requireReady || isButtonReady(button));
      });
      if (candidate) return candidate;
    }
    return null;
  }

  function requiredDraftFieldsAreReady(expectedCategoryId) {
    const titleInput = document.querySelector('#mountedActionSchema0\\.title');
    const categorySelect = document.querySelector('#mountedActionSchema0\\.news_category_id');
    const editor = document.querySelector('.tiptap.ProseMirror');
    return Boolean(titleInput?.value.trim()
      && categorySelect?.value === expectedCategoryId
      && editor?.textContent.trim()
      && findDocumentDeleteButtons().length === 0);
  }

  async function createPublication(expectedCategoryId) {
    await waitUntil(
      () => requiredDraftFieldsAreReady(expectedCategoryId),
      WAIT_TIMEOUT_MS,
      'required publication fields',
    );
    await delay(500);
    await waitUntil(
      () => findPublicationCreateButton(),
      UPLOAD_TIMEOUT_MS,
      'the publication Create button',
    );
    const submitButton = await waitUntil(
      () => findPublicationCreateButton({ requireReady: true }),
      UPLOAD_TIMEOUT_MS,
      'the publication Create button to become ready',
    );
    const dialog = getCreateDialog();
    submitButton.click();
    await waitUntil(
      () => !dialog?.isConnected || dialog.getClientRects().length === 0,
      SUBMIT_TIMEOUT_MS,
      'publication creation to finish',
    );
  }

  async function autofill() {
    const draftId = getParam('af_draft_id');
    if (!draftId) return;
    const publicationToken = getParam('af_publish_token');
    let publicationCreated = false;
    try {
      const draft = await fetchDraft(draftId);
      const createButton = await waitFor('[wire\\:click="mountAction(\'create\')"]');
      createButton.click();
      await Promise.all([
        waitFor('#mountedActionSchema0\\.title'),
        waitFor('#mountedActionSchema0\\.news_category_id'),
        waitFor('.tiptap.ProseMirror'),
      ]);
      const removedDocuments = await removeDocumentRows();
      const [titleInput, categorySelect, editor] = await Promise.all([
        waitFor('#mountedActionSchema0\\.title'),
        waitFor('#mountedActionSchema0\\.news_category_id'),
        waitFor('.tiptap.ProseMirror'),
      ]);
      if (draft.title) setNativeValue(titleInput, draft.title);
      const expectedCategoryId = String(draft.category_id || DEFAULT_CATEGORY_ID);
      setNativeValue(categorySelect, expectedCategoryId);
      if (draft.source_url) {
        const sourceInput = document.querySelector('#mountedActionSchema0\\.source')
          || document.querySelector('#mountedActionSchema0\\.source_url')
          || document.querySelector('[wire\\:model="mountedActions.0.data.source"]');
        if (sourceInput) setNativeValue(sourceInput, draft.source_url);
      }
      if (draft.text) await fillEditor(editor, draft.text);
      if (draft.photo_url) await setPhoto(draft.photo_url);
      await createPublication(expectedCategoryId);
      publicationCreated = true;
      await reportPublication(draftId, publicationToken, true);
      console.info(`[tg2site] Publication created; removed document rows: ${removedDocuments}.`);
    } catch (error) {
      console.error('[tg2site] Autofill failed:', error);
      if (!publicationCreated && publicationToken) {
        try {
          await reportPublication(draftId, publicationToken, false, error.message);
        } catch (reportError) {
          console.error('[tg2site] Could not report publication failure:', reportError);
        }
      }
      if (publicationCreated) {
        window.alert('Публикация создана, но сервис не получил подтверждение.\n'
          + 'Не отправляйте форму повторно — проверьте сайт и локальный сервис.');
      } else {
        window.alert(`Не удалось автоматически создать публикацию: ${error.message}\n`
          + 'Форма оставлена открытой для проверки.');
      }
    }
  }

  if (document.readyState === 'loading') {
    window.addEventListener('DOMContentLoaded', autofill, { once: true });
  } else {
    autofill();
  }
})();
