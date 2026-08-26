"""Optional browser-side enhancements.

Everything here is strictly an improvement on behaviour that already works
without it. The server renders complete, usable pages; this script makes
three of the interactions feel quicker. If it fails to parse, fails to fetch,
or is blocked entirely, every one of those interactions falls back to the
plain link or form it was built on.

That constraint is what makes an untested script acceptable: it can only fail
*closed*. It is deliberately small, has no build step, and no dependencies.

1. Back to top appears only once there is something to scroll back over.
2. The jump lists become real dropdowns that navigate on selection.
3. Picking a slot swaps the grid in place instead of reloading the page, so
   you keep your exact position. The new markup still comes from the server,
   so there is no second copy of the rendering rules to drift out of step.
"""

from __future__ import annotations

import base64
import hashlib

#: Injected inline. No external file, so nothing extra to fetch or cache-bust.
SCRIPT = """
(function () {
  'use strict';

  var SCROLL_THRESHOLD = 300;

  // Honour the reader's motion preference wherever we scroll for them.
  function behavior() {
    var query = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)');
    return query && query.matches ? 'auto' : 'smooth';
  }

  function backToTop() {
    var button = document.querySelector('.to-top');
    if (!button) { return; }

    function sync() {
      var scrolled = window.pageYOffset || document.documentElement.scrollTop;
      button.classList.toggle('is-hidden', scrolled < SCROLL_THRESHOLD);
    }
    sync();
    window.addEventListener('scroll', sync, { passive: true });

    // Scroll here rather than leaving it to the #top fragment, so the
    // animation is ours to control and does not depend on a root-level
    // scroll-behavior rule. Without the script the plain link still works.
    button.addEventListener('click', function (event) {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) {
        return;
      }
      event.preventDefault();
      window.scrollTo({ top: 0, behavior: behavior() });
    });
  }

  // The <details> list works on its own; a <select> just saves a tap.
  function dropdowns(root) {
    var lists = root.querySelectorAll('details.jump');
    Array.prototype.forEach.call(lists, function (details) {
      var links = details.querySelectorAll('.jump-list a');
      if (!links.length) { return; }

      var summary = details.querySelector('summary');
      var caption = summary ? summary.textContent.trim() : '';

      var select = document.createElement('select');
      select.className = 'jump-select';
      select.setAttribute('aria-label', caption);

      var first = document.createElement('option');
      first.value = '';
      first.textContent = caption;
      select.appendChild(first);

      Array.prototype.forEach.call(links, function (link) {
        var option = document.createElement('option');
        option.value = link.getAttribute('href');
        option.textContent = link.textContent.trim();
        if (link.getAttribute('aria-current') === 'true') {
          option.selected = true;
        }
        select.appendChild(option);
      });

      select.addEventListener('change', function () {
        var value = select.value;
        if (!value) { return; }
        if (value.charAt(0) === '#') {
          var target = document.getElementById(value.slice(1));
          if (target) { target.scrollIntoView({ behavior: behavior(), block: 'start' }); }
        } else {
          window.location.href = value;
        }
      });

      details.parentNode.replaceChild(select, details);
    });
  }

  // Replace <main> with the server's own rendering of the next state, without
  // losing the scroll position. Any failure falls through to a normal
  // navigation, which is what would have happened anyway.
  function swap(url) {
    var main = document.getElementById('main');
    if (!main || !window.fetch || !window.DOMParser || !window.history.pushState) {
      window.location.href = url;
      return;
    }
    var keep = window.pageYOffset || document.documentElement.scrollTop;

    fetch(url, { credentials: 'same-origin' })
      .then(function (response) {
        if (!response.ok) { throw new Error('bad status'); }
        return response.text();
      })
      .then(function (html) {
        var fresh = new DOMParser()
          .parseFromString(html, 'text/html')
          .getElementById('main');
        if (!fresh) { throw new Error('no main'); }
        main.innerHTML = fresh.innerHTML;
        window.history.pushState({}, '', url);
        window.scrollTo(0, keep);
        bind(main);
        document.documentElement.setAttribute('data-swapped', 'true');
      })
      .catch(function () { window.location.href = url; });
  }

  function bind(root) {
    dropdowns(root);
    var actions = root.querySelectorAll('a.slot-action[href^="/day?"]');
    Array.prototype.forEach.call(actions, function (link) {
      link.addEventListener('click', function (event) {
        // Leave modified clicks alone: they mean "open elsewhere".
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) {
          return;
        }
        event.preventDefault();
        swap(link.getAttribute('href'));
      });
    });
  }

  function start() {
    document.documentElement.setAttribute('data-enhanced', 'true');
    backToTop();
    bind(document);
    window.addEventListener('popstate', function () {
      window.location.reload();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
"""


def _csp_hash(source: str) -> str:
    """The CSP source expression that authorises exactly this script.

    The page's Content-Security-Policy is ``default-src 'self'``, which blocks
    inline scripts -- it silently blocked this one, so the enhancements never
    ran at all while every HTML assertion still passed. Authorising the exact
    bytes by hash keeps the policy strict: no ``'unsafe-inline'``, and any
    other inline script, injected or accidental, is still refused.
    """
    digest = hashlib.sha256(source.encode("utf-8")).digest()
    return f"sha256-{base64.b64encode(digest).decode('ascii')}"


#: Recomputed on import, so editing SCRIPT can never leave the policy stale.
SCRIPT_CSP_HASH = _csp_hash(SCRIPT)
