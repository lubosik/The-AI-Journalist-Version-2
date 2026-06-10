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
    } else if (hasSidebarError(sidebar) || !hasThreadItems(sidebar)) {
      injectSidebarEmptyState();
    }
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
    });
    _observer.observe(target, { childList: true, subtree: true });
  }

  function init() {
    applyBranding();
    setTimeout(function () {
      syncOrbital();
      syncSidebar();
      startObserver();
    }, 150);
    // Re-check sidebar after a longer delay to catch post-render empty states
    setTimeout(syncSidebar, 1500);
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

})();
