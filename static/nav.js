/* ── Raven-Tech Hamburger Nav ─────────────────────────────────────────
   Shared across all pages. Auto-highlights the current page.
   ──────────────────────────────────────────────────────────────────── */
(function () {
  "use strict";

  var NAV_ITEMS = [
    { href: "/",                label: "Home",            desc: "Platform overview and engine directory" },
    { href: "/market-intelligence", label: "Market Intelligence", desc: "Pre-open roadmap and cross-asset stress" },
    { href: "/breach",          label: "Engine 1",        desc: "Earnings hold risk with Monte Carlo" },
    { href: "/spx",             label: "Engine 2",        desc: "SPX/SPY iron condor scanner" },
    { href: "/lead-lag",        label: "Engine 5",        desc: "Global lead-lag regime intelligence" },
    { href: "/red-dog",         label: "Red Dog",         desc: "Mean-reversion scanner (SP500 + NDX)" },
    { href: "/ichimoku",        label: "Ichimoku",        desc: "Trend-continuation scanner" },
    { href: "/pairs",           label: "Engine 7",        desc: "Thematic relative value pairs scanner" },
    { href: "/post-event",      label: "Engine 8",        desc: "Post-event trade extension evaluator" },
    { href: "/credit-stress",   label: "Engine 9",        desc: "Credit stress drift detection" },
    { href: "/calendar",        label: "Earnings Calendar", desc: "Mega-cap earnings dates and compare workflow" },
    { href: "/compare",         label: "Compare",         desc: "Multi-ticker side-by-side" },
    { href: "/news-risk",       label: "News Risk",       desc: "Macro events and headline risk" },
    { href: "/flow-monitor",    label: "Flow Monitor",    desc: "Prediction market unusual activity detection" },
  ];

  /* Which nav item matches the current URL? */
  function isActive(href) {
    var p = window.location.pathname;
    if (href === "/") return p === "/" || p === "";
    return p === href || p.startsWith(href + "/");
  }

  /* Build the drawer once DOM is ready */
  function init() {
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
      a.className = "navDrawerLink" + (isActive(item.href) ? " navDrawerLink--active" : "");
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
