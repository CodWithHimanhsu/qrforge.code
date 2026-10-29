/* QRForge front end. Plain JavaScript, no dependencies, no external requests.
 * Talks only to the local Flask server (POST /api/generate). */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const form = $('qr-form');

  // Approximate maximum bytes of byte-mode data per error-correction level (QR version 40).
  const MAX_BYTES = { L: 2953, M: 2331, Q: 1663, H: 1273 };
  const EC_RECOVERY = { L: '7%', M: '15%', Q: '25%', H: '30%' };

  const PRESETS = {
    balanced: { format: 'png', size: '1024', ec: 'M', fg: '#000000', bg: '#ffffff', border: '4',
      hint: 'Standard square modules, black on white, medium error correction. A good default for most uses.' },
    print: { format: 'svg', size: '1024', ec: 'M', fg: '#000000', bg: '#ffffff', border: '4',
      hint: 'SVG output scales without blur. Keep the printed code large enough (a few centimetres or more) and test-scan a proof.' },
    robust: { format: 'png', size: '2048', ec: 'H', fg: '#000000', bg: '#ffffff', border: '6',
      hint: 'High error correction with a wider margin. The code gets denser, so print it larger.' },
  };

  const state = { busy: false, objectUrl: null, hasResult: false, payload: '' };

  // ---------- helpers ----------
  const currentType = () => form.querySelector('input[name="content-type"]:checked').value;
  const fieldsType = (type) => (type === 'sms' ? 'phone' : type);
  const radioValue = (name) => form.querySelector(`input[name="${name}"]:checked`).value;
  const val = (id) => $(id).value;
  const trimmed = (id) => $(id).value.trim();

  function setStatus(message, isError = false) {
    const el = $('status');
    el.textContent = message;
    el.classList.toggle('is-error', isError);
  }

  function showFormError(message, fieldId) {
    clearInvalid();
    const el = $('form-error');
    el.textContent = message;
    el.hidden = false;
    if (fieldId && $(fieldId)) {
      $(fieldId).setAttribute('aria-invalid', 'true');
      $(fieldId).focus();
    }
  }

  function clearInvalid() {
    form.querySelectorAll('[aria-invalid]').forEach((el) => el.removeAttribute('aria-invalid'));
    $('form-error').hidden = true;
    $('form-error').textContent = '';
  }

  // ---------- content-type sections ----------
  function showType() {
    const active = fieldsType(currentType());
    form.querySelectorAll('.type-fields').forEach((section) => {
      section.hidden = section.dataset.type !== active;
    });
    clearInvalid();
  }

  function updateWifiPasswordVisibility() {
    $('wifi-password-field').hidden = val('wifi-security') === 'nopass';
  }

  function updatePhoneMode() {
    $('phone-message-field').hidden = radioValue('phone-mode') !== 'sms';
  }

  // ---------- URL normalization (mirrors the server so the user sees what will be encoded) ----------
  const BARE_HOST = /^(localhost|([a-z0-9\u00a1-\uffff-]+\.)+[a-z\u00a1-\uffff]{2,})([:/?#].*)?$/i;

  function previewUrl(raw) {
    const value = raw.trim();
    if (!value) return { url: '', note: '' };
    if (!value.includes('://') && BARE_HOST.test(value)) {
      return { url: 'https://' + value, note: 'https:// will be added.' };
    }
    return { url: value, note: '' };
  }

  function updateUrlPreview() {
    const { url, note } = previewUrl(val('url-url'));
    $('url-final').textContent = url && note ? `Will encode: ${url}` : '';
  }

  function isValidHttpUrl(value) {
    if (/\s/.test(value)) return false;
    try {
      const parsed = new URL(value);
      return (parsed.protocol === 'http:' || parsed.protocol === 'https:') && parsed.hostname !== '';
    } catch (_) {
      return false;
    }
  }

  // ---------- text capacity guidance ----------
  function updateTextCount() {
    const bytes = new TextEncoder().encode(val('text-text')).length;
    const max = MAX_BYTES[val('error-correction')];
    const over = bytes > max;
    $('text-count').textContent =
      `${bytes.toLocaleString()} bytes used. A QR code holds roughly ${max.toLocaleString()} bytes at level ${val('error-correction')}` +
      ' (non-English letters use 2 to 4 bytes each).' + (over ? ' This is probably too long: shorten it or link to it with a short URL.' : '');
  }

  // ---------- settings, presets, colors ----------
  function readSettings() {
    return {
      format: radioValue('format'),
      size: val('size'),
      ec: val('error-correction'),
      fg: val('foreground').toLowerCase(),
      bg: val('background').toLowerCase(),
      border: val('border'),
    };
  }

  function applyPreset(name) {
    const p = PRESETS[name];
    form.querySelector(`input[name="format"][value="${p.format}"]`).checked = true;
    $('size').value = p.size;
    $('error-correction').value = p.ec;
    $('foreground').value = p.fg;
    $('background').value = p.bg;
    $('border').value = p.border;
    syncSettingsUi();
  }

  function syncPresetIndicator() {
    const now = readSettings();
    const match = Object.keys(PRESETS).find((name) =>
      ['format', 'size', 'ec', 'fg', 'bg', 'border'].every((k) => PRESETS[name][k] === now[k]));
    form.querySelectorAll('input[name="preset"]').forEach((radio) => { radio.checked = radio.value === match; });
    $('preset-hint').textContent = match ? PRESETS[match].hint
      : 'Custom settings. Pick a preset above to return to a tested starting point.';
  }

  function luminance(hex) {
    const channel = (i) => {
      const s = parseInt(hex.slice(i, i + 2), 16) / 255;
      return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
    };
    return 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5);
  }

  function updateContrastHint() {
    const fg = val('foreground');
    const bg = val('background');
    $('foreground-value').textContent = fg.toUpperCase();
    $('background-value').textContent = bg.toUpperCase();
    const [hi, lo] = [luminance(fg), luminance(bg)].sort((a, b) => b - a);
    const ratio = (hi + 0.05) / (lo + 0.05);
    let text = `Contrast about ${ratio.toFixed(1)}:1.`;
    if (luminance(fg) > luminance(bg)) text += ' Light-on-dark codes are not read by some scanners.';
    else if (ratio < 4) text += ' This is low and may make scanning harder. This is an estimate, not a guarantee.';
    $('contrast-hint').textContent = text;
  }

  function syncSettingsUi() {
    const isPng = radioValue('format') === 'png';
    $('size').disabled = !isPng;
    updateContrastHint();
    updateTextCount();
    syncPresetIndicator();
  }

  // ---------- collecting and validating ----------
  function collectRequest() {
    const uiType = currentType();
    let type = uiType;
    let data;
    switch (uiType) {
      case 'url':
        data = { url: previewUrl(val('url-url')).url };
        break;
      case 'text':
        data = { text: val('text-text') };
        break;
      case 'wifi':
        data = { ssid: val('wifi-ssid'), security: val('wifi-security'),
                 password: val('wifi-password'), hidden: $('wifi-hidden').checked };
        break;
      case 'contact':
        data = { full_name: trimmed('contact-full_name'), phone: trimmed('contact-phone'),
                 email: trimmed('contact-email'), organization: trimmed('contact-organization'),
                 website: trimmed('contact-website') };
        break;
      case 'email':
        data = { to: trimmed('email-to'), subject: trimmed('email-subject'), body: val('email-body') };
        break;
      case 'phone':
        type = radioValue('phone-mode');
        data = { number: trimmed('phone-number') };
        if (type === 'sms') data.message = val('phone-message');
        break;
      default:
        throw new Error('Unknown content type');
    }
    return {
      type, data,
      format: radioValue('format'),
      size: parseInt(val('size'), 10),
      error_correction: val('error-correction'),
      foreground: val('foreground').toUpperCase(),
      background: val('background').toUpperCase(),
      border: parseInt(val('border'), 10),
    };
  }

  const EMAIL_RE = /^[^@\s<>,;:"\\]+@[^@\s<>,;:"\\]+\.[^@\s<>,;:"\\]{2,}$/;

  function validatePhone(raw) {
    if (!/^\+?[0-9\s().\-]+$/.test(raw)) return false;
    const digits = raw.replace(/\D/g, '').length;
    return digits >= 3 && digits <= 15;
  }

  /** Quick checks for a good experience. The server validates again and is authoritative. */
  function validateClient(request) {
    const { type, data } = request;
    switch (type) {
      case 'url':
        if (!data.url) return ['Enter a website address.', 'url-url'];
        if (!isValidHttpUrl(data.url)) return ['Enter a full web address beginning with http:// or https://.', 'url-url'];
        return null;
      case 'text':
        return data.text.trim() ? null : ['Enter some text to encode.', 'text-text'];
      case 'wifi':
        if (!data.ssid.trim()) return ['Enter the network name (SSID).', 'wifi-ssid'];
        if (data.security !== 'nopass') {
          if (!data.password) return ['Enter the password, or choose "No password".', 'wifi-password'];
          if (data.security === 'WPA' && !/^[0-9a-fA-F]{64}$/.test(data.password) &&
              (data.password.length < 8 || data.password.length > 63)) {
            return ['A WPA password must be 8 to 63 characters long.', 'wifi-password'];
          }
        }
        return null;
      case 'contact':
        if (!data.full_name) return ['Enter a name for the contact.', 'contact-full_name'];
        if (data.phone && !validatePhone(data.phone)) return ['Enter a valid phone number.', 'contact-phone'];
        if (data.email && !EMAIL_RE.test(data.email)) return ['Enter a valid email address.', 'contact-email'];
        if (data.website && !isValidHttpUrl(previewUrl(data.website).url)) return ['Enter a valid website address.', 'contact-website'];
        return null;
      case 'email':
        if (!data.to) return ['Enter the recipient email address.', 'email-to'];
        return EMAIL_RE.test(data.to) ? null : ['Enter a valid email address.', 'email-to'];
      case 'phone':
      case 'sms':
        if (!data.number) return ['Enter a phone number.', 'phone-number'];
        return validatePhone(data.number) ? null : ['Enter a valid phone number (3 to 15 digits).', 'phone-number'];
      default:
        return null;
    }
  }

  // ---------- generate ----------
  function setBusy(busy) {
    state.busy = busy;
    const button = $('generate-btn');
    button.disabled = busy;
    button.textContent = busy ? 'Generating…' : 'Generate QR code';
    form.setAttribute('aria-busy', String(busy));
    $('loading').hidden = !busy;
    if (busy) {
      $('empty-state').hidden = true;
      $('qr-img').hidden = true;
    } else {
      $('empty-state').hidden = state.hasResult;
      $('qr-img').hidden = !state.hasResult;
    }
  }

  function base64ToBlob(base64, mimeType) {
    const binary = atob(base64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    return new Blob([bytes], { type: mimeType });
  }

  async function onSubmit(event) {
    event.preventDefault();
    if (state.busy) return; // ignore duplicate submissions while one is in flight

    clearInvalid();
    const request = collectRequest();
    const problem = validateClient(request);
    if (problem) {
      showFormError(problem[0], problem[1]);
      setStatus('');
      return;
    }

    setBusy(true);
    setStatus('');
    try {
      const response = await fetch('/api/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(request),
      });
      let body = null;
      try { body = await response.json(); } catch (_) { /* non-JSON error page */ }

      if (response.status === 401) {
        // The login session ended: reload, and the sign-in page signs in again with the saved password.
        showFormError('Your session has ended. Reloading to sign in again…');
        window.setTimeout(() => window.location.reload(), 1200);
        return;
      }
      if (!response.ok) {
        const error = (body && body.error) || {};
        const fieldName = error.field ? error.field.replace(/^data\./, '') : null;
        const fieldId = fieldName ? `${fieldsType(currentType())}-${fieldName}` : null;
        showFormError(error.message || 'The server could not generate this code. Please try again.', fieldId);
        setStatus('Could not generate the code. Your input is unchanged.', true);
        return;
      }
      renderResult(body);
    } catch (_) {
      showFormError('Could not reach the QRForge server. Make sure the app is still running in your terminal, then try again.');
      setStatus('Could not reach the local server.', true);
    } finally {
      setBusy(false);
    }
  }

  function renderResult(result) {
    if (state.objectUrl) URL.revokeObjectURL(state.objectUrl);
    state.objectUrl = URL.createObjectURL(base64ToBlob(result.image_base64, result.mime_type));
    state.hasResult = true;
    state.payload = result.payload;

    $('qr-img').src = state.objectUrl;
    $('preview-stage').dataset.state = 'ready';
    $('stale-note').hidden = true;

    $('meta-format').textContent = result.format.toUpperCase();
    $('meta-size').textContent = result.pixel_size
      ? `${result.pixel_size} × ${result.pixel_size} px`
      : 'Vector (scales to any size)';
    $('meta-version').textContent = `Version ${result.version} (${result.modules} × ${result.modules} modules)`;
    $('meta-ec').textContent = `${result.error_correction} (about ${EC_RECOVERY[result.error_correction]})`;
    $('meta-border').textContent = `${result.quiet_zone} modules`;

    const warnings = $('warnings');
    warnings.replaceChildren();
    result.warnings.forEach((message) => {
      const item = document.createElement('li');
      item.textContent = message;
      warnings.appendChild(item);
    });
    warnings.hidden = result.warnings.length === 0;

    const download = $('download-btn');
    download.href = state.objectUrl;
    download.setAttribute('download', result.filename);
    download.textContent = `Download ${result.format.toUpperCase()}`;

    $('payload-text').textContent = result.payload;
    $('result').hidden = false;
    setStatus(`QR code generated. Ready to download as ${result.filename}.`);
  }

  function markStale() {
    if (!state.hasResult) return;
    $('stale-note').hidden = false;
    $('preview-stage').dataset.state = 'stale';
  }

  // ---------- copy ----------
  async function copyPayload() {
    if (!state.hasResult) return;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(state.payload);
      } else {
        const temp = document.createElement('textarea');
        temp.value = state.payload;
        temp.setAttribute('readonly', '');
        temp.className = 'sr-copy';
        document.body.appendChild(temp);
        temp.select();
        const ok = document.execCommand('copy');
        temp.remove();
        if (!ok) throw new Error('copy failed');
      }
      setStatus('Encoded data copied to the clipboard.');
    } catch (_) {
      $('result').querySelector('details').open = true;
      setStatus('Copying was blocked by the browser. The encoded data is shown below so you can copy it manually.', true);
    }
  }

  // ---------- clear ----------
  function clearAll() {
    form.reset();
    if (state.objectUrl) URL.revokeObjectURL(state.objectUrl);
    state.objectUrl = null;
    state.hasResult = false;
    state.payload = '';
    $('qr-img').removeAttribute('src');
    $('qr-img').hidden = true;
    $('result').hidden = true;
    $('payload-text').textContent = '';
    $('empty-state').hidden = false;
    $('preview-stage').dataset.state = 'empty';
    $('wifi-password').type = 'password';
    clearInvalid();
    setStatus('');
    showType();
    updateWifiPasswordVisibility();
    updatePhoneMode();
    updateUrlPreview();
    syncSettingsUi();
  }

  // ---------- wiring ----------
  const logoutButton = $('logout-btn'); // only present when a password is set
  if (logoutButton) {
    logoutButton.addEventListener('click', async () => {
      try { await fetch('/api/logout', { method: 'POST' }); } catch (_) { /* reload anyway */ }
      try { window.localStorage.removeItem('qrforge.password'); } catch (_) { /* ignore */ }
      window.location.reload();
    });
  }

  form.addEventListener('submit', onSubmit);
  $('clear-btn').addEventListener('click', clearAll);
  $('copy-btn').addEventListener('click', copyPayload);

  form.addEventListener('input', markStale);
  form.addEventListener('change', markStale);

  form.querySelectorAll('input[name="content-type"]').forEach((r) => r.addEventListener('change', showType));
  form.querySelectorAll('input[name="phone-mode"]').forEach((r) => r.addEventListener('change', updatePhoneMode));
  form.querySelectorAll('input[name="preset"]').forEach((r) => r.addEventListener('change', () => applyPreset(r.value)));
  form.querySelectorAll('input[name="format"]').forEach((r) => r.addEventListener('change', syncSettingsUi));
  ['size', 'error-correction', 'foreground', 'background', 'border'].forEach((id) => $(id).addEventListener('input', syncSettingsUi));

  $('wifi-security').addEventListener('change', updateWifiPasswordVisibility);
  $('wifi-show-password').addEventListener('change', (e) => { $('wifi-password').type = e.target.checked ? 'text' : 'password'; });
  $('url-url').addEventListener('input', updateUrlPreview);
  $('text-text').addEventListener('input', updateTextCount);
  form.addEventListener('input', (e) => { if (e.target.removeAttribute) e.target.removeAttribute('aria-invalid'); });

  showType();
  updateWifiPasswordVisibility();
  updatePhoneMode();
  syncSettingsUi();
})();
