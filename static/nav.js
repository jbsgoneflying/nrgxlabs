/* ── Raven-Tech Hamburger Nav ─────────────────────────────────────────
   Shared across all pages. Auto-highlights the current page.
   ──────────────────────────────────────────────────────────────────── */
(function () {
  "use strict";

  // Desk-trimmed nav: only the engines the desk uses daily. E1b and
  // E2b remain inset under their parent scanners. Other engines still
  // work via direct URL; they're just hidden from the drawer to
  // reduce noise. To re-expose one, add it back here.
  var NAV_ITEMS = [
    { href: "/",                label: "Market Intelligence", desc: "Pre-open roadmap and cross-asset stress" },
    { href: "/breach",          label: "Engine 1",  desc: "Earnings hold risk with Monte Carlo" },
    { href: "/earnings-ic",     label: "Engine 1b", desc: "Earnings IC scenario replay",        sub: true },
    { href: "/spx",             label: "Engine 2",  desc: "SPX/SPY iron condor scanner" },
    { href: "/ic-scenario",     label: "Engine 2b", desc: "SPX/SPY IC scenario replay",         sub: true },
    { href: "/post-event",      label: "Engine 7",  desc: "Post-event trade extension evaluator" },
    { href: "/compare",         label: "Engine 10", desc: "Multi-ticker side-by-side" },
  ];

  /* Which nav item matches the current URL? */
  function isActive(href) {
    var p = window.location.pathname;
    if (href === "/") return p === "/" || p === "" || p === "/market-intelligence";
    return p === href || p.startsWith(href + "/");
  }

  /* Inject sub-engine styling once (keeps this change self-contained
     in nav.js so styles.css doesn't need a cache-buster bump across
     every page that loads the nav). */
  function injectSubStyles() {
    if (document.getElementById("navDrawerSubStyles")) return;
    var s = document.createElement("style");
    s.id = "navDrawerSubStyles";
    s.textContent =
      ".navDrawerLink--sub{" +
        "margin-left:18px;padding-left:16px;" +
        "border-left:2px solid rgba(52,199,89,0.22);" +
        "border-top-left-radius:0;border-bottom-left-radius:0;" +
      "}" +
      ".navDrawerLink--sub .navDrawerLinkLabel{" +
        "font-size:13px;font-weight:600;color:rgba(11,11,15,0.78);" +
      "}" +
      ".navDrawerLink--sub .navDrawerLinkDesc{font-size:10.5px;}" +
      ".navDrawerLink--sub.navDrawerLink--active{" +
        "border-left-color:rgba(52,199,89,0.55);" +
      "}";
    document.head.appendChild(s);
  }

  /* Build the drawer once DOM is ready */
  function init() {
    injectSubStyles();

    /* ── Hamburger button (injected into .appHeader) ── */
    var header = document.querySelector(".appHeader");
    if (!header) return;

    /* Remove old inline topNav if present */
    var oldNav = header.querySelector(".topNav");
    if (oldNav) oldNav.remove();

    var btn = document.createElement("button");
    btn.className = "navHamburger";
    btn.setAttribute("aria-label", "Open navigation");
    btn.innerHTML = '<span></span><span></span><span></span>';
    header.appendChild(btn);

    /* ── Overlay ── */
    var overlay = document.createElement("div");
    overlay.className = "navOverlay";
    document.body.appendChild(overlay);

    /* ── Drawer ── */
    var drawer = document.createElement("nav");
    drawer.className = "navDrawer";
    drawer.setAttribute("aria-label", "Main navigation");

    /* Drawer header */
    var dHead = document.createElement("div");
    dHead.className = "navDrawerHead";
    dHead.innerHTML =
      '<img class="navDrawerLogo" src="/static/RavenONLY.png" alt="" />' +
      '<div><div class="navDrawerTitle">Raven-Tech.co</div>' +
      '<div class="navDrawerSub">Quantitative Desk Intelligence</div></div>';
    drawer.appendChild(dHead);

    /* Links */
    var list = document.createElement("div");
    list.className = "navDrawerLinks";
    NAV_ITEMS.forEach(function (item) {
      var a = document.createElement("a");
      a.href = item.href;
      var cls = "navDrawerLink";
      if (item.sub) cls += " navDrawerLink--sub";
      if (isActive(item.href)) cls += " navDrawerLink--active";
      a.className = cls;
      a.innerHTML =
        '<span class="navDrawerLinkLabel">' + item.label + '</span>' +
        '<span class="navDrawerLinkDesc">' + item.desc + '</span>';
      list.appendChild(a);
    });
    drawer.appendChild(list);

    document.body.appendChild(drawer);

    /* ── Toggle logic ── */
    function open()  { drawer.classList.add("open"); overlay.classList.add("open"); btn.classList.add("open"); }
    function close() { drawer.classList.remove("open"); overlay.classList.remove("open"); btn.classList.remove("open"); }

    btn.addEventListener("click", function () {
      drawer.classList.contains("open") ? close() : open();
    });
    overlay.addEventListener("click", close);

    /* Close on Escape */
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") close();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
