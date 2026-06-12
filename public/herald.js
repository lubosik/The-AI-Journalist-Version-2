/* HERALD Intelligence — UI Enhancements v2 */
(function () {
  'use strict';

  /* ── Page title & placeholder ── */
  function applyBranding() {
    if (document.title !== 'HERALD Intelligence') {
      document.title = 'HERALD Intelligence';
    }
    var ta = document.querySelector('textarea');
    if (ta && ta.placeholder !== 'Ask HERALD anything. Type / for commands...') {
      ta.placeholder = 'Ask HERALD anything. Type / for commands...';
    }
    // Hide Chainlit branding links (text or href based)
    document.querySelectorAll('a').forEach(function (link) {
      var t = (link.textContent || '').trim().toLowerCase();
      var h = (link.href || '').toLowerCase();
      if ((t.includes('chainlit') || h.includes('chainlit')) && !t.includes('herald')) {
        link.style.cssText += 'display:none!important;visibility:hidden!important;';
      }
    });
    // Rename login heading
    document.querySelectorAll('h1, h2').forEach(function (h) {
      if ((h.textContent || '').trim() === 'Login to access the app') {
        h.textContent = 'HERALD Intelligence';
        if (!h.nextElementSibling || !h.nextElementSibling.classList.contains('herald-login-subtitle')) {
          var sub = document.createElement('p');
          sub.className = 'herald-login-subtitle';
          sub.textContent = 'Login to access your private intelligence workspace';
          sub.style.cssText = 'font-size:14px;color:#9a9488;margin:4px 0 0;';
          h.insertAdjacentElement('afterend', sub);
        }
      }
    });
  }

  /* ── Orbital H empty state ── */
  var _orbitalInjected = false;

  function createOrbital() {
    var el = document.createElement('div');
    el.id = 'herald-orbital-empty';
    el.className = 'herald-empty-state';
    el.innerHTML = [
      '<div class="herald-orbital-wrapper" id="herald-orbital">',
      '  <div class="herald-ring herald-ring-1"></div>',
      '  <div class="herald-ring herald-ring-2"></div>',
      '  <div class="herald-ring herald-ring-3"></div>',
      '  <span class="herald-h">H</span>',
      '</div>',
      '<div class="herald-empty-title">HERALD Intelligence</div>',
      '<div class="herald-empty-subtitle">',
      "  Drop a link, a rumour, or a topic.<br>I'll find the VC secondaries angle.",
      '</div>',
    ].join('');
    return el;
  }

  function findChatArea() {
    return (
      document.querySelector('[class*="message-container"]') ||
      document.querySelector('[class*="chat-messages"]') ||
      document.querySelector('[class*="messages-list"]')
    );
  }

  function hasMessages() {
    var area = findChatArea();
    if (!area) return false;
    var msgs = area.querySelectorAll(
      '[class*="message"][class*="user"], [class*="message"][class*="assistant"], ' +
      '[class*="human-message"], [class*="ai-message"]'
    );
    return msgs.length > 0;
  }

  function injectOrbital() {
    // DOM is the source of truth — prevents duplicate injection across MutationObserver firings
    if (document.getElementById('herald-orbital-empty')) return;
    if (_orbitalInjected) return;
    if (hasMessages()) return;
    var area = findChatArea();
    if (!area) return;
    // Do not inject inside a starters/suggestions container
    if (area.className && /starter|starters|suggestion/i.test(area.className)) return;
    area.appendChild(createOrbital());
    _orbitalInjected = true;
  }

  function removeOrbital() {
    var el = document.getElementById('herald-orbital-empty');
    if (!el) { _orbitalInjected = false; return; }
    el.style.transition = 'opacity 0.3s ease, transform 0.3s ease';
    el.style.opacity = '0';
    el.style.transform = 'scale(0.94)';
    setTimeout(function () {
      if (el.parentNode) el.parentNode.removeChild(el);
      _orbitalInjected = false;
    }, 320);
  }

  function setProcessing(active) {
    var wrapper = document.getElementById('herald-orbital');
    if (!wrapper) return;
    if (active) {
      wrapper.classList.add('herald-processing');
    } else {
      wrapper.classList.remove('herald-processing');
    }
  }

  function syncOrbital() {
    if (hasMessages()) {
      if (_orbitalInjected) removeOrbital();
    } else {
      if (!_orbitalInjected) injectOrbital();
    }
    var area = findChatArea();
    if (area) {
      var running = area.querySelector('[data-status="running"]') ||
                    area.querySelector('[data-running="true"]');
      setProcessing(!!running);
    }
  }

  /* ── Sidebar empty-state handling ── */
  var _sidebarEmptyInjected = false;

  function findSidebar() {
    return (
      document.querySelector('[class*="thread-list"]') ||
      document.querySelector('[class*="sidebar"] [class*="threads"]') ||
      document.querySelector('nav [class*="list"]') ||
      document.querySelector('[class*="history"]')
    );
  }

  function hasThreadItems(sidebar) {
    if (!sidebar) return false;
    return sidebar.querySelectorAll('a[href*="thread"], [class*="thread-item"], [class*="ThreadItem"]').length > 0;
  }

  function hasSidebarError(sidebar) {
    if (!sidebar) return false;
    // Look for error states or infinite loading spinners with no content
    var hasSpinner = !!sidebar.querySelector('[class*="loading"], [class*="spinner"], [role="progressbar"]');
    var hasError = !!sidebar.querySelector('[class*="error"]');
    return (hasSpinner || hasError) && !hasThreadItems(sidebar);
  }

  function injectSidebarEmptyState() {
    var sidebar = findSidebar();
    if (!sidebar) return;
    if (hasThreadItems(sidebar)) {
      removeSidebarEmptyState();
      return;
    }
    if (document.getElementById('herald-sidebar-empty')) return;
    if (_sidebarEmptyInjected) return;

    // Hide any error/spinner elements
    sidebar.querySelectorAll('[class*="loading"], [class*="spinner"], [role="progressbar"], [class*="error"]').forEach(function (el) {
      el.style.display = 'none';
    });

    var emptyEl = document.createElement('div');
    emptyEl.id = 'herald-sidebar-empty';
    emptyEl.style.cssText = [
      'padding: 20px 16px;',
      'text-align: center;',
      'color: #9a9488;',
      'font-size: 13px;',
      'font-family: Inter, sans-serif;',
      'line-height: 1.5;',
    ].join('');
    emptyEl.innerHTML = [
      '<div style="font-size:24px;margin-bottom:8px;opacity:0.5;">H</div>',
      '<div style="font-weight:600;color:#c9a84c;margin-bottom:4px;">No conversations yet</div>',
      '<div>Start a new conversation<br>to begin your research session.</div>',
    ].join('');
    sidebar.appendChild(emptyEl);
    _sidebarEmptyInjected = true;
  }

  function removeSidebarEmptyState() {
    var el = document.getElementById('herald-sidebar-empty');
    if (el && el.parentNode) el.parentNode.removeChild(el);
    _sidebarEmptyInjected = false;
  }

  function syncSidebar() {
    var sidebar = findSidebar();
    if (!sidebar) return;
    if (hasThreadItems(sidebar)) {
      removeSidebarEmptyState();
    } else if (!hasSidebarError(sidebar)) {
      // Only inject empty state when not loading — hasSidebarError detects active spinners
      injectSidebarEmptyState();
    }
  }

  function formatUtcLabel(isoString) {
    try {
      var d = new Date(isoString);
      return d.toLocaleString('en-GB', {
        day: '2-digit',
        month: 'short',
        year: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        timeZone: 'America/New_York',
        hour12: false
      }) + ' ET';
    } catch (_) {
      return '';
    }
  }

  function decorateMessageTimestamps() {
    var messages = document.querySelectorAll(
      '[class*="message"][class*="user"], [class*="message"][class*="assistant"], ' +
      '[class*="human-message"], [class*="ai-message"]'
    );
    messages.forEach(function (messageEl) {
      if (messageEl.querySelector('.herald-message-time')) return;
      var timestamp = messageEl.getAttribute('data-herald-ts');
      if (!timestamp) {
        var existingTime = messageEl.querySelector('time');
        timestamp = existingTime && existingTime.getAttribute('datetime');
      }
      if (!timestamp) {
        timestamp = new Date().toISOString();
        messageEl.setAttribute('data-herald-ts', timestamp);
      }
      var footer = document.createElement('div');
      footer.className = 'herald-message-time';
      footer.textContent = formatUtcLabel(timestamp);
      messageEl.appendChild(footer);
    });
  }

  function urlBase64ToUint8Array(base64String) {
    var padding = '='.repeat((4 - base64String.length % 4) % 4);
    var base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    var rawData = window.atob(base64);
    return Uint8Array.from(Array.from(rawData).map(function (char) {
      return char.charCodeAt(0);
    }));
  }

  async function enableNotifications(button) {
    try {
      var permission = await Notification.requestPermission();
      if (permission !== 'granted') {
        button.textContent = 'Notifications blocked';
        return;
      }
      var registration = await navigator.serviceWorker.ready;
      var configResponse = await fetch('/herald/push/config');
      if (!configResponse.ok) throw new Error('Push configuration unavailable');
      var config = await configResponse.json();
      var subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(config.publicKey)
      });
      var response = await fetch('/herald/push/subscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ subscription: subscription.toJSON() })
      });
      if (!response.ok) throw new Error('Subscription failed');
      button.textContent = 'Notifications enabled';
      button.disabled = true;
    } catch (_) {
      button.textContent = 'Enable notifications';
    }
  }

  function injectNotificationButton() {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) return;
    if (document.getElementById('herald-enable-notifications')) return;
    var sidebar = findSidebar();
    if (!sidebar) return;
    var button = document.createElement('button');
    button.id = 'herald-enable-notifications';
    button.type = 'button';
    button.textContent = 'Enable notifications';
    button.disabled = false;
    button.style.cssText = [
      'width:calc(100% - 24px)',
      'margin:12px',
      'padding:10px 12px',
      'border-radius:999px',
      'border:1px solid rgba(201,168,76,0.28)',
      'background:rgba(201,168,76,0.08)',
      'color:#c9a84c',
      'font:500 12px Inter,sans-serif',
      'cursor:pointer'
    ].join(';');
    button.addEventListener('click', function () {
      enableNotifications(button);
    });
    sidebar.appendChild(button);
    if (Notification.permission === 'granted') {
      navigator.serviceWorker.ready.then(function (registration) {
        return registration.pushManager.getSubscription();
      }).then(function (subscription) {
        if (subscription) {
          button.textContent = 'Notifications enabled';
          button.disabled = true;
        }
      }).catch(function () {});
    }
  }

  var _workspaceUsers = null;

  async function getWorkspaceUsers() {
    if (_workspaceUsers) return _workspaceUsers;
    try {
      var response = await fetch('/herald/workspace/users');
      var payload = await response.json();
      _workspaceUsers = payload.users || [];
    } catch (_) {
      _workspaceUsers = [];
    }
    return _workspaceUsers;
  }

  function hideMentionMenu() {
    var menu = document.getElementById('herald-mention-menu');
    if (menu) menu.remove();
  }

  async function showMentionMenu(textarea) {
    var users = await getWorkspaceUsers();
    hideMentionMenu();
    if (!users.length) return;
    var menu = document.createElement('div');
    menu.id = 'herald-mention-menu';
    menu.style.cssText = [
      'position:fixed',
      'z-index:9999',
      'left:50%',
      'bottom:92px',
      'transform:translateX(-50%)',
      'width:min(420px,calc(100vw - 32px))',
      'padding:8px',
      'border:1px solid rgba(201,168,76,0.28)',
      'border-radius:14px',
      'background:rgba(7,7,15,0.97)',
      'box-shadow:0 18px 48px rgba(0,0,0,0.45)'
    ].join(';');
    users.forEach(function (user) {
      var option = document.createElement('button');
      option.type = 'button';
      option.textContent = '@' + user.identifier + '  ' + user.displayName;
      option.style.cssText = 'display:block;width:100%;padding:10px;border:0;background:transparent;color:#f2eee6;text-align:left;cursor:pointer;';
      option.addEventListener('click', function () {
        var cursor = textarea.selectionStart;
        var before = textarea.value.slice(0, cursor);
        var after = textarea.value.slice(cursor);
        textarea.value = before.replace(/@[\w.-]*$/, '@' + user.identifier + ' ') + after;
        textarea.focus();
        hideMentionMenu();
      });
      menu.appendChild(option);
    });
    document.body.appendChild(menu);
  }

  function bindMentionPicker() {
    var textarea = document.querySelector('textarea');
    if (!textarea || textarea.dataset.heraldMentionsBound) return;
    textarea.dataset.heraldMentionsBound = 'true';
    textarea.addEventListener('input', function () {
      var before = textarea.value.slice(0, textarea.selectionStart);
      if (/@[\w.-]*$/.test(before)) {
        showMentionMenu(textarea);
      } else {
        hideMentionMenu();
      }
    });
  }

  var _observer = null;

  function startObserver() {
    if (_observer) return;
    var target = document.querySelector('[class*="chat"]') ||
                 document.querySelector('main') ||
                 document.body;
    if (!target) return;
    _observer = new MutationObserver(function () {
      syncOrbital();
      applyBranding();
      syncSidebar();
      decorateMessageTimestamps();
      injectNotificationButton();
      bindMentionPicker();
    });
    _observer.observe(target, { childList: true, subtree: true });
  }

  function init() {
    applyBranding();
    setTimeout(function () {
      syncOrbital();
      syncSidebar();
      decorateMessageTimestamps();
      injectNotificationButton();
      bindMentionPicker();
      startObserver();
    }, 150);
    // Re-check sidebar after short delay
    setTimeout(syncSidebar, 1500);
    // Force empty state after 6s if sidebar is still spinning (DB unreachable)
    setTimeout(function () {
      var sidebar = findSidebar();
      if (sidebar && !hasThreadItems(sidebar) && !document.getElementById('herald-sidebar-empty')) {
        injectSidebarEmptyState();
      }
    }, 6000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  window.addEventListener('popstate', function () {
    _orbitalInjected = false;
    setTimeout(init, 200);
  });

  /* ── PWA: manifest + service worker ── */
  function addPWAMeta() {
    var metas = [
      ['apple-mobile-web-app-capable', 'yes'],
      ['apple-mobile-web-app-status-bar-style', 'black-translucent'],
      ['apple-mobile-web-app-title', 'HERALD'],
      ['theme-color', '#c9a84c'],
      ['mobile-web-app-capable', 'yes'],
    ];
    metas.forEach(function (pair) {
      if (!document.querySelector('meta[name="' + pair[0] + '"]')) {
        var meta = document.createElement('meta');
        meta.name = pair[0];
        meta.content = pair[1];
        document.head.appendChild(meta);
      }
    });
    if (!document.querySelector('link[rel="manifest"]')) {
      var link = document.createElement('link');
      link.rel = 'manifest';
      link.href = '/public/manifest.json';
      document.head.appendChild(link);
    }
  }

  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/public/sw.js').catch(function () {});
  }

  addPWAMeta();

  /* ── MODEL SWITCHER DROPDOWN ────────────────────────────────────────── */
  var HERALD_MODELS = [
    { key: 'hermes',        label: 'Hermes (Default)',    desc: 'GPT-4o via OpenRouter' },
    { key: 'claude-sonnet', label: 'Claude Sonnet 4.6',  desc: 'Best for writing & analysis' },
    { key: 'claude-opus',   label: 'Claude Opus 4.8',    desc: 'Most capable — deep reasoning' },
    { key: 'gpt-4o',        label: 'GPT-4o',             desc: 'Fast and reliable' },
    { key: 'gemini-flash',  label: 'Gemini 2.5 Flash',   desc: 'Fastest responses' },
    { key: 'perplexity',    label: 'Perplexity Sonar',   desc: 'Live web search' },
  ];
  var _selectedModel = localStorage.getItem('herald_model') || 'hermes';
  var _modelDropdownOpen = false;

  function _closeModelDropdown() {
    var d = document.getElementById('herald-model-dropdown');
    if (d) d.remove();
    _modelDropdownOpen = false;
  }

  function _openModelDropdown() {
    _closeModelDropdown();
    _modelDropdownOpen = true;
    var dropdown = document.createElement('div');
    dropdown.id = 'herald-model-dropdown';
    dropdown.style.cssText = [
      'position:fixed', 'bottom:80px', 'right:20px', 'z-index:9999',
      'background:rgba(7,7,15,0.97)', 'backdrop-filter:blur(20px)',
      'border:1px solid rgba(255,255,255,0.1)', 'border-radius:12px',
      'padding:8px', 'min-width:240px',
      'box-shadow:0 16px 48px rgba(0,0,0,0.6)',
    ].join(';');

    var hdr = document.createElement('div');
    hdr.style.cssText = 'padding:6px 10px 10px;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:rgba(201,168,76,0.7);font-family:Inter,sans-serif;';
    hdr.textContent = 'Select Model';
    dropdown.appendChild(hdr);

    HERALD_MODELS.forEach(function (m) {
      var isCurrent = m.key === _selectedModel;
      var item = document.createElement('div');
      item.style.cssText = [
        'display:flex', 'align-items:center', 'gap:10px',
        'padding:10px 12px', 'border-radius:8px', 'cursor:pointer',
        'background:' + (isCurrent ? 'rgba(201,168,76,0.1)' : 'transparent'),
        'border:1px solid ' + (isCurrent ? 'rgba(201,168,76,0.3)' : 'transparent'),
        'margin-bottom:2px', 'transition:all 0.1s ease',
      ].join(';');
      item.innerHTML = '<div style="flex:1;">'
        + '<div style="font-size:13px;font-weight:500;color:' + (isCurrent ? '#c9a84c' : '#f0ece4') + ';font-family:Inter,sans-serif;">' + m.label + '</div>'
        + '<div style="font-size:11px;color:#7a7468;font-family:Inter,sans-serif;margin-top:1px;">' + m.desc + '</div>'
        + '</div>'
        + (isCurrent ? '<div style="width:6px;height:6px;border-radius:50%;background:#c9a84c;"></div>' : '');

      item.addEventListener('mouseenter', function () { if (!isCurrent) item.style.background = 'rgba(255,255,255,0.04)'; });
      item.addEventListener('mouseleave', function () { if (!isCurrent) item.style.background = 'transparent'; });
      item.addEventListener('click', function (e) {
        e.stopPropagation();
        _selectedModel = m.key;
        localStorage.setItem('herald_model', m.key);
        _closeModelDropdown();
        _showModelBadge(m.label);
        fetch('/api/model/switch', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ model_key: m.key }),
        }).catch(function () {});
      });
      dropdown.appendChild(item);
    });

    document.body.appendChild(dropdown);
    setTimeout(function () {
      document.addEventListener('click', function _outside(e) {
        var d2 = document.getElementById('herald-model-dropdown');
        if (d2 && !d2.contains(e.target)) { _closeModelDropdown(); }
        document.removeEventListener('click', _outside);
      });
    }, 10);
  }

  function _showModelBadge(label) {
    var badge = document.getElementById('herald-model-badge');
    if (!badge) {
      badge = document.createElement('div');
      badge.id = 'herald-model-badge';
      badge.style.cssText = [
        'position:fixed', 'bottom:58px', 'right:20px',
        'background:rgba(201,168,76,0.1)', 'border:1px solid rgba(201,168,76,0.3)',
        'border-radius:20px', 'padding:4px 12px 4px 8px',
        'font-size:11px', 'font-family:JetBrains Mono,monospace',
        'color:#c9a84c', 'display:flex', 'align-items:center', 'gap:6px',
        'z-index:100', 'pointer-events:none',
      ].join(';');
      badge.innerHTML = '<div style="width:5px;height:5px;border-radius:50%;background:#c9a84c;"></div><span id="herald-model-name"></span>';
      document.body.appendChild(badge);
    }
    var nameEl = document.getElementById('herald-model-name');
    if (nameEl) nameEl.textContent = label;
  }

  function _wireModelButton() {
    document.querySelectorAll(
      '[data-id="model"], [class*="command-item"][title*="model" i], button[aria-label*="model" i]'
    ).forEach(function (btn) {
      if (btn.dataset._heraldWired) return;
      btn.dataset._heraldWired = '1';
      btn.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();
        if (_modelDropdownOpen) { _closeModelDropdown(); } else { _openModelDropdown(); }
      });
    });
  }

  // Show current model badge on load
  var _currentModelLabel = (HERALD_MODELS.find(function (m) { return m.key === _selectedModel; }) || HERALD_MODELS[0]).label;
  setTimeout(function () { _showModelBadge(_currentModelLabel); }, 1500);

  var _modelBtnObserver = new MutationObserver(function () { _wireModelButton(); });
  _modelBtnObserver.observe(document.body, { childList: true, subtree: true });
  setTimeout(_wireModelButton, 800);
  /* ─────────────────────────────────────────────────────────────────── */

})();
