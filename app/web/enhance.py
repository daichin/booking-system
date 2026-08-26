"""Optional browser-side enhancements.

Everything here is strictly an improvement on behaviour that already works
without it. The server renders complete, usable pages; this script makes a
few of the interactions feel quicker. If it fails to parse, fails to fetch,
or is blocked entirely, every one of those interactions falls back to the
plain link or form it was built on.

That constraint is what makes a lightly tested script acceptable: it can only
fail *closed*. It is deliberately small, has no build step and no
dependencies, and is authorised in the Content-Security-Policy by the hash of
its exact bytes rather than by relaxing the policy.

1. Back to top appears only once there is something to scroll back over, and
   scrolls in JavaScript -- a root-level `scroll-behavior: smooth` silently
   breaks fragment jumps to the top of a document.
2. The jump lists become real dropdowns that act on selection.
3. Moving around the grid -- picking a slot, cancelling a selection, changing
   day or week -- swaps the page contents in place instead of reloading, so
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
  // Links that move around inside the grid. Nav links are plain "/day" with
  // no query string, so they never match and always navigate normally.
  var SWAPPABLE = 'a[href^="/day?"], a[href^="/week?"]';

  function each(list, fn) { Array.prototype.forEach.call(list, fn); }

  function main() { return document.getElementById('main'); }

  // Honour the reader's motion preference wherever we scroll for them.
  function behavior() {
    var query = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)');
    return query && query.matches ? 'auto' : 'smooth';
  }

  function supported() {
    return !!(window.fetch && window.DOMParser && window.history.pushState && window.URL);
  }

  // Reading a layout property forces the browser to lay the new content out.
  // Without it scrollTo clamps against a document that has not been measured
  // yet and lands at 0, which is exactly what changing day used to do.
  function afterLayout(fn) {
    void document.body.offsetHeight;
    fn();
    if (window.requestAnimationFrame) { window.requestAnimationFrame(fn); }
  }

  function restore(y) {
    afterLayout(function () { window.scrollTo(0, y); });
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

    button.addEventListener('click', function (event) {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) { return; }
      event.preventDefault();
      window.scrollTo({ top: 0, behavior: behavior() });
    });
  }

  function go(url) {
    var target = new URL(url, window.location.href);
    if (target.hash && target.pathname === window.location.pathname
        && target.search === window.location.search) {
      var element = document.getElementById(target.hash.slice(1));
      if (element) {
        element.scrollIntoView({ behavior: behavior(), block: 'start' });
        return;
      }
    }
    swap(url);
  }

  // The <details> list works on its own; a <select> just saves a tap.
  function dropdowns(root) {
    each(root.querySelectorAll('details.jump'), function (details) {
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

      each(links, function (link) {
        var option = document.createElement('option');
        option.value = link.getAttribute('href');
        option.textContent = link.textContent.trim();
        if (link.getAttribute('aria-current') === 'true') { option.selected = true; }
        select.appendChild(option);
      });

      select.addEventListener('change', function () {
        if (!select.value) { return; }
        if (select.value.charAt(0) === '#') {
          var element = document.getElementById(select.value.slice(1));
          if (element) { element.scrollIntoView({ behavior: behavior(), block: 'start' }); }
        } else {
          go(select.value);
        }
      });

      details.parentNode.replaceChild(select, details);
    });
  }

  // Replace the page contents with the server's own rendering of the next
  // state. Any failure falls through to a normal navigation, which is what
  // would have happened anyway.
  function swap(url) {
    var host = main();
    if (!host || !supported()) { window.location.href = url; return; }

    var target = new URL(url, window.location.href);
    var samePage = target.pathname === window.location.pathname;
    var keep = window.pageYOffset || document.documentElement.scrollTop;

    fetch(url, { credentials: 'same-origin' })
      .then(function (response) {
        if (!response.ok) { throw new Error('bad status'); }
        return response.text();
      })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, 'text/html');
        var fresh = doc.getElementById('main');
        if (!fresh) { throw new Error('no main'); }

        host.innerHTML = fresh.innerHTML;

        // The nav lives outside <main>, so its "you are here" marker would
        // otherwise still point at the page you came from.
        var nav = document.querySelector('.site-header .site-nav');
        var freshNav = doc.querySelector('.site-header .site-nav');
        if (nav && freshNav) { nav.innerHTML = freshNav.innerHTML; }
        if (doc.title) { document.title = doc.title; }

        window.history.pushState({}, '', url);

        if (samePage) {
          // Staying put is the whole point: picking a slot or cancelling a
          // selection must not move the page under you.
          restore(keep);
        } else {
          var anchor = target.hash ? document.getElementById(target.hash.slice(1)) : null;
          afterLayout(function () {
            if (anchor) { anchor.scrollIntoView({ block: 'start' }); }
            else { window.scrollTo(0, 0); }
          });
        }

        bind();
        document.documentElement.setAttribute('data-swapped', 'true');
      })
      .catch(function () { window.location.href = url; });
  }

  function links(root) {
    each(root.querySelectorAll(SWAPPABLE), function (link) {
      link.addEventListener('click', function (event) {
        // Leave modified clicks alone: they mean "open elsewhere".
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) {
          return;
        }
        event.preventDefault();
        go(link.getAttribute('href'));
      });
    });
  }

  function forms(root) {
    if (!window.FormData || !window.URLSearchParams) { return; }
    each(root.querySelectorAll('form.date-jump'), function (form) {
      form.addEventListener('submit', function (event) {
        event.preventDefault();
        var query = new URLSearchParams(new FormData(form)).toString();
        go(form.getAttribute('action') + (query ? '?' + query : ''));
      });
    });
  }

  function bind() {
    var host = main();
    if (!host) { return; }
    dropdowns(host);
    links(host);
    forms(host);
  }

  function start() {
    document.documentElement.setAttribute('data-enhanced', 'true');
    // Otherwise the browser restores its own idea of the scroll position
    // after pushState, undoing the position we just put back and dropping
    // the reader at the top of the page.
    if ('scrollRestoration' in window.history) {
      window.history.scrollRestoration = 'manual';
    }
    backToTop();
    bind();
    // The swapped-in state is not restorable from history, so going back
    // re-fetches the page rather than showing a stale grid.
    window.addEventListener('popstate', function () { window.location.reload(); });
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
