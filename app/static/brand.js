/* Logo + icon styling shared by index.html and login.html.
   The server stores the styles (branding.py); this turns them into CSS for
   the wordmark and into an SVG for icon previews. Keep iconSvg() in step with
   branding.icon_svg(). */
(function () {
  function rgba(hex, a) {
    var n = parseInt((hex || "#ffffff").slice(1), 16);
    return "rgba(" + ((n >> 16) & 255) + "," + ((n >> 8) & 255) + "," + (n & 255) + "," + a + ")";
  }

  function logoCss(st) {
    st = st || {};
    var colors = (st.colors && st.colors.length) ? st.colors : ["#8b5cf6", "#f5a142"];
    var size = st.pattern_size || 6, half = size / 2;
    var pc = rgba(st.pattern_color || "#ffffff", (st.pattern_opacity || 35) / 100);
    var patterns = {
      stripes:  ["repeating-linear-gradient(90deg, " + pc + " 0 " + half + "px, transparent " + half + "px " + size + "px)", "auto"],
      diagonal: ["repeating-linear-gradient(45deg, " + pc + " 0 " + half + "px, transparent " + half + "px " + size + "px)", "auto"],
      dots:     ["radial-gradient(circle, " + pc + " 35%, transparent 37%)", size + "px " + size + "px"],
      checker:  ["conic-gradient(" + pc + " 25%, transparent 0 50%, " + pc + " 0 75%, transparent 0)", (size * 2) + "px " + (size * 2) + "px"],
      grid:     ["linear-gradient(" + pc + " 1px, transparent 1px), linear-gradient(90deg, " + pc + " 1px, transparent 1px)",
                 size + "px " + size + "px, " + size + "px " + size + "px"]
    };
    var css = {
      background: "none", backgroundSize: "", WebkitBackgroundClip: "", backgroundClip: "",
      color: "", WebkitTextFillColor: "", WebkitTextStroke: "", filter: ""
    };
    if (st.fill === "outline") {
      css.color = "transparent";
      css.WebkitTextFillColor = "transparent";
      css.WebkitTextStroke = (st.outline_width || 2) + "px " + colors[0];
    } else {
      var base = st.fill === "solid"
        ? "linear-gradient(" + colors[0] + ", " + colors[0] + ")"
        : "linear-gradient(" + (st.angle == null ? 90 : st.angle) + "deg, " + colors.join(", ") + ")";
      var p = patterns[st.pattern];
      css.background = p ? p[0] + ", " + base : base;
      css.backgroundSize = p ? p[1] + ", auto" : "auto";
      css.WebkitBackgroundClip = "text";
      css.backgroundClip = "text";
      css.color = "transparent";
      css.WebkitTextFillColor = "transparent";
    }
    if (st.glow) css.filter = "drop-shadow(0 0 6px " + rgba(colors[0], 0.65) + ")";
    return css;
  }

  function applyLogo(el, st) {
    if (!el) return;
    var css = logoCss(st);
    for (var k in css) el.style[k] = css[k];
  }

  var TILES = [];
  [6.5, 13.5, 20.5].forEach(function (y, r) {
    [6.5, 13.5, 20.5].forEach(function (x, c) { TILES.push([x, y, (r * 3 + c) % 2 === 0]); });
  });

  function iconSvg(st) {
    st = st || {};
    var colors = (st.colors && st.colors.length) ? st.colors : ["#8b5cf6", "#f5a142"];
    var c1 = colors[0], c2 = st.fill === "gradient" ? colors[colors.length - 1] : c1;
    var rad = (st.angle == null ? 135 : st.angle) * Math.PI / 180;
    var len = 16 * (Math.abs(Math.sin(rad)) + Math.abs(Math.cos(rad)));
    var dx = len * Math.sin(rad), dy = -len * Math.cos(rad);
    var shape = st.shape || "rounded";
    var bg = shape === "circle" ? '<circle cx="16" cy="16" r="16" fill="url(#g)"/>'
      : '<rect width="32" height="32" rx="' + (shape === "square" ? 0 : 7) + '" fill="url(#g)"/>';
    var s = shape === "circle" ? 0.85 : 1, t = 16 * (1 - s);
    var tiles = TILES.map(function (tl) {
      return '<rect x="' + tl[0] + '" y="' + tl[1] + '" width="5.5" height="5.5" rx="1.2"' + (tl[2] ? "" : ' opacity="0.4"') + '/>';
    }).join("");
    return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">' +
      '<defs><linearGradient id="g" gradientUnits="userSpaceOnUse" x1="' + (16 - dx) + '" y1="' + (16 - dy) +
      '" x2="' + (16 + dx) + '" y2="' + (16 + dy) + '"><stop offset="0" stop-color="' + c1 +
      '"/><stop offset="1" stop-color="' + c2 + '"/></linearGradient></defs>' + bg +
      '<g fill="' + (st.tile_color || "#ffffff") + '" transform="translate(' + t + ' ' + t + ') scale(' + s + ')">' + tiles + '</g></svg>';
  }

  // point the favicon / touch icon at the current version so caches refresh
  function setIconVersion(v) {
    var q = "?v=" + encodeURIComponent(v || "1");
    var fav = document.querySelector('link[rel="icon"]');
    if (fav) fav.href = "/icon/favicon" + q;
    var touch = document.querySelector('link[rel="apple-touch-icon"]');
    if (touch) touch.href = "/icon/apple-touch-icon.png" + q;
  }

  window.SpazBrand = { logoCss: logoCss, applyLogo: applyLogo, iconSvg: iconSvg, setIconVersion: setIconVersion };
})();
