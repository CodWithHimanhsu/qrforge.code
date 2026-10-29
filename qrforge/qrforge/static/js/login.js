/* QRForge sign-in page. Saves the password in this browser (if "Remember" is
 * ticked) and signs in automatically on the next visit. Talks only to this
 * server (POST /api/login). */
(() => {
  'use strict';

  const STORAGE_KEY = 'qrforge.password';
  const $ = (id) => document.getElementById(id);
  const form = $('login-form');
  const input = $('password');
  const remember = $('remember');
  const button = $('login-btn');
  const errorBox = $('login-error');
  const status = $('login-status');
  let busy = false;

  // localStorage can be blocked (private mode, strict settings), so every access is guarded.
  const saved = {
    get() { try { return window.localStorage.getItem(STORAGE_KEY); } catch (_) { return null; } },
    set(value) { try { window.localStorage.setItem(STORAGE_KEY, value); } catch (_) { /* ignore */ } },
    clear() { try { window.localStorage.removeItem(STORAGE_KEY); } catch (_) { /* ignore */ } },
  };

  function showError(message) {
    errorBox.textContent = message;
    errorBox.hidden = false;
    status.textContent = '';
  }

  async function signIn(password, isAutomatic) {
    if (busy) return;
    busy = true;
    button.disabled = true;
    errorBox.hidden = true;
    status.textContent = isAutomatic ? 'Signing in with your saved password…' : 'Checking…';

    try {
      const response = await fetch('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password }),
      });
      let body = null;
      try { body = await response.json(); } catch (_) { /* not JSON */ }

      if (response.ok) {
        if (remember.checked) saved.set(password); else saved.clear();
        status.textContent = 'Signed in. Loading…';
        window.location.reload();
        return;
      }

      const error = (body && body.error) || {};
      if (response.status === 401) {
        // A stored password that no longer works (it was changed) must not be retried forever.
        if (isAutomatic) {
          saved.clear();
          input.value = '';
          showError('Your saved password no longer works. Please enter the current password.');
        } else {
          showError('Incorrect password. Please try again.');
        }
        input.focus();
      } else {
        showError(error.message || 'Could not sign in. Please try again.');
      }
    } catch (_) {
      showError('Could not reach the server. Check your connection and try again.');
    } finally {
      busy = false;
      button.disabled = false;
    }
  }

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    if (!input.value) {
      showError('Enter the password.');
      input.focus();
      return;
    }
    signIn(input.value, false);
  });

  $('show-password').addEventListener('change', (event) => {
    input.type = event.target.checked ? 'text' : 'password';
  });

  // Auto-fill and sign in with the saved password (one attempt per page load).
  const storedPassword = saved.get();
  if (storedPassword) {
    input.value = storedPassword;
    signIn(storedPassword, true);
  } else {
    input.focus();
  }
})();
