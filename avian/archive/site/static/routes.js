(function (root, factory) {
  "use strict";

  var routes = factory();

  if (typeof module === "object" && module.exports) {
    module.exports = routes;
  }

  if (root) {
    root.ListeningGardenRoutes = routes;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  var SLUG_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
  var DETECTION_ID_PATTERN = /^[a-f0-9]{64}$/;
  var REVIEW_VALUES = ["pending", "unreviewed", "confirmed", "uncertain", "rejected"];
  var CONFIDENCE_VALUES = ["0", "0.8", "0.9"];
  var EXPLORE_FIELDS = [
    "q",
    "species",
    "date_from",
    "confidence_min",
    "review",
  ];

  function routePath(pathname) {
    if (typeof pathname !== "string") {
      return null;
    }

    var path = pathname.split(/[?#]/, 1)[0];
    if (path === "/birds" || path === "/birds/") {
      return "/";
    }
    if (path.indexOf("/birds/") === 0) {
      return path.slice(6);
    }
    return path;
  }

  function parseRoute(pathname) {
    var path = routePath(pathname);
    if (path === "/" || path === "" || path === "/index.html" || path === "/index.html/") {
      return { name: "today" };
    }
    if (path === "/explore" || path === "/explore/") {
      return { name: "explore" };
    }
    if (path === "/species" || path === "/species/") {
      return { name: "species-index" };
    }
    if (path === "/about" || path === "/about/") {
      return { name: "about" };
    }

    var speciesMatch = /^\/species\/([^/]+)\/?$/.exec(path || "");
    if (speciesMatch && SLUG_PATTERN.test(speciesMatch[1])) {
      return { name: "species-detail", slug: speciesMatch[1] };
    }

    var detectionMatch = /^\/detection\/([^/]+)\/?$/.exec(path || "");
    if (detectionMatch && DETECTION_ID_PATTERN.test(detectionMatch[1])) {
      return { name: "detection-detail", detectionId: detectionMatch[1] };
    }

    return { name: "not-found" };
  }

  function href(name, params) {
    params = params || {};
    if (name === "today") return "/birds/";
    if (name === "explore") return "/birds/explore";
    if (name === "species-index") return "/birds/species";
    if (name === "about") return "/birds/about";
    if (name === "species-detail" && SLUG_PATTERN.test(params.slug || "")) {
      return "/birds/species/" + params.slug;
    }
    if (
      name === "detection-detail" &&
      DETECTION_ID_PATTERN.test(params.detectionId || "")
    ) {
      return "/birds/detection/" + params.detectionId;
    }
    throw new Error("Invalid route or route parameters");
  }

  function legacyDetectionHref(pathname, search) {
    if (parseRoute(pathname).name !== "today") return null;
    var params = new URLSearchParams(typeof search === "string" ? search : "");
    var detectionId = params.get("detection") || "";
    if (!DETECTION_ID_PATTERN.test(detectionId)) return null;
    return href("detection-detail", { detectionId: detectionId });
  }

  function parseExplore(search) {
    var params = new URLSearchParams(typeof search === "string" ? search : "");
    var result = {};
    EXPLORE_FIELDS.forEach(function (field) {
      result[field] = params.get(field) || "";
    });
    if (REVIEW_VALUES.indexOf(result.review) === -1) {
      result.review = "";
    }
    if (CONFIDENCE_VALUES.indexOf(result.confidence_min) === -1) {
      result.confidence_min = "";
    }

    var rawPage = params.get("page");
    var page = /^\d+$/.test(rawPage || "") ? Number(rawPage) : 1;
    result.page = Number.isSafeInteger(page) && page >= 1 ? page : 1;
    return result;
  }

  function exploreSearch(state) {
    state = state || {};
    var params = new URLSearchParams();
    EXPLORE_FIELDS.forEach(function (field) {
      var value = state[field];
      if (field === "review" && REVIEW_VALUES.indexOf(value) === -1) return;
      if (field === "confidence_min" && CONFIDENCE_VALUES.indexOf(value) === -1) return;
      if (typeof value === "string" && value !== "") {
        params.append(field, value);
      }
    });

    var page = Number(state.page);
    if (Number.isSafeInteger(page) && page >= 1) {
      params.append("page", String(page));
    }
    var query = params.toString();
    return query ? "?" + query : "";
  }

  return {
    parseRoute: parseRoute,
    href: href,
    legacyDetectionHref: legacyDetectionHref,
    parseExplore: parseExplore,
    exploreSearch: exploreSearch,
  };
});
