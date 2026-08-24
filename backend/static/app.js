(function () {
  "use strict";

  // sample forecast (illustrative only, matches the shape of a
  // real forecast data, see fetchAndRenderForecast())

  function dayCount(start, end) {
    var s = new Date(start.getFullYear(), start.getMonth(), start.getDate());
    var e = new Date(end.getFullYear(), end.getMonth(), end.getDate());
    return Math.round((e - s) / 86400000) + 1;
  }

  function toISODate(d) {
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
  }

  function parseISODate(s) {
    var parts = s.split("-").map(Number);
    return new Date(parts[0], parts[1] - 1, parts[2]);
  }

  // A saved range is only worth restoring if it hasn't already passed,
  // otherwise a range picked a week ago would silently reappear pointed
  // at days that are now in the past. When it's stale, today's freshly
  // computed "tomorrow + 2 days" default (set further down) is left alone.
  function restoreRangeIfUpcoming(savedStart, savedEnd) {
    if (!savedStart || !savedEnd) return;
    var today = new Date();
    var today0 = new Date(today.getFullYear(), today.getMonth(), today.getDate());
    var e = parseISODate(savedEnd);
    if (e < today0) return;
    rangeStart = parseISODate(savedStart);
    rangeEnd = e;
  }

  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // returning-user system persistence (localStorage)
  // Only the STABLE facts about someone's setup persist: panel,
  // quantity, location, last engine choice. The date range never
  // does, since "tomorrow + 2 days" saved from a week ago is just wrong
  // today, so that always starts fresh (see rangeStart/rangeEnd
  // further down). Saved once a forecast actually succeeds (see
  // revealDashboard()), not on every keystroke through the wizard.
  var SYSTEM_KEY = "wattcast-system";

  function loadSavedSystem() {
    try {
      return JSON.parse(localStorage.getItem(SYSTEM_KEY) || "null");
    } catch (e) {
      return null;
    }
  }

  function saveSystem() {
    var engineEl = document.querySelector(".engine-option.selected");
    localStorage.setItem(SYSTEM_KEY, JSON.stringify({
      manufacturer: flow.manufacturer,
      model: flow.model,
      partId: flow.partId,
      modelWatts: flow.modelWatts,
      arimaSupported: flow.arimaSupported,
      qty: flow.qty,
      location: flow.location,
      latitude: flow.latitude,
      longitude: flow.longitude,
      engine: engineEl ? engineEl.dataset.engine : "random_forest",
      rangeStart: toISODate(rangeStart),
      rangeEnd: toISODate(rangeEnd),
    }));
  }

  // The splash's dismissal and the /api/panels fetch race
  // independently (one is user-timed, the other is network-timed), so
  // don't decide whether to show the wizard or skip straight to a
  // returning user's dashboard until both have actually finished.
  var splashDone = false, panelsReady = false, savedSystem = null;

  function selectManufacturerChip(name) {
    var chip = document.querySelector('#manufacturer-chips .chip[data-name="' + String(name).toLowerCase() + '"]');
    if (chip) chip.classList.add("selected");
  }

  function maybeEnterApp() {
    if (!splashDone || !panelsReady) return;

    // The URL is authoritative over which view wins, not just whichever
    // localStorage key happens to exist. A completed system is
    // normally enough to jump straight to the dashboard, but if the
    // hash itself says "mid-edit of the wizard" (e.g. this is a reload
    // right after clicking "Edit setup"), that has to win instead, or
    // refreshing on a wizard step silently bounces back to the
    // dashboard underneath it.
    var setupHashMatch = /^#setup\/(\d)$/.exec(location.hash);

    if (savedSystem && savedSystem.partId && !setupHashMatch) {
      Object.assign(flow, savedSystem);
      restoreRangeIfUpcoming(savedSystem.rangeStart, savedSystem.rangeEnd);
      selectManufacturerChip(savedSystem.manufacturer);
      setArimaAvailable(!!savedSystem.arimaSupported);
      if (savedSystem.engine) {
        document.querySelectorAll(".engine-option").forEach(function (o) {
          o.classList.toggle("selected", o.dataset.engine === savedSystem.engine);
        });
      }
      showLoadingScreen(revealDashboard);
      return;
    }

    document.getElementById("flow-view").hidden = false;
    // topbar-reset-btn's visibility is step-driven (see goToStep).
    // Nothing to reset yet on step 1, so it stays hidden by default
    // here and is only shown once goToStep() lands past it.
    if (savedSystem && savedSystem.partId) {
      // Editing an existing setup, not building a brand new one, so the
      // dashboard-only chrome underneath (from before the reload) must
      // not stay showing over the wizard, and the engine choice (not
      // part of the draft below) has to come from the saved system or
      // it'd silently revert to the default on every such reload.
      document.getElementById("edit-setup-btn").hidden = true;
      document.getElementById("topbar-performance-btn").hidden = true;
      document.getElementById("clock-context").hidden = true;
      if (savedSystem.engine) {
        document.querySelectorAll(".engine-option").forEach(function (o) {
          o.classList.toggle("selected", o.dataset.engine === savedSystem.engine);
        });
      }
    }

    // No completed setup, but maybe an in-progress one. Pick up
    // wherever it was left (step and answers both), not step 1 blank.
    // goToStep() below already saves a draft on every step (including
    // from backToSetup()), so editing an existing setup lands here too.
    var draft = loadDraft();
    if (draft && draft.step) {
      Object.assign(flow, draft);
      restoreRangeIfUpcoming(draft.rangeStart, draft.rangeEnd);
      if (draft.manufacturer) selectManufacturerChip(draft.manufacturer);
      if (draft.manufacturer) {
        document.getElementById("step1-next").disabled = false;
        renderModelList("");
      }
      if (draft.partId) {
        document.getElementById("step2-next").disabled = false;
        setArimaAvailable(!!draft.arimaSupported);
      }
      updateQtyReadout();
      if (draft.location) {
        document.getElementById("location-search").value = draft.location;
        if (draft.latitude != null) showLocationFound(draft.location);
      }
      renderDayStrip("wizard-day-strip", selectDateFromWizard); // needed here too now that step 5 is a realistic landing step, not just reachable by clicking through
      goToStep(draft.step); // sets the URL hash too, even on a fresh tab with no hash yet
    }
  }

  // Browser back/forward between #setup/N steps.
  window.addEventListener("hashchange", function () {
    if (location.hash === "#dashboard") {
      // Forward-navigated from a setup step back to the dashboard.
      // It's already rendered from before, just re-show it rather
      // than re-fetching anything.
      if (document.getElementById("dashboard-view").style.display === "none") {
        document.getElementById("dashboard-view").style.display = "";
        document.getElementById("flow-view").hidden = true;
        document.getElementById("edit-setup-btn").hidden = false;
        document.getElementById("topbar-performance-btn").hidden = false;
        document.getElementById("topbar-reset-btn").hidden = true;
      }
      return;
    }
    var m = /^#setup\/(\d)$/.exec(location.hash);
    if (!m) return;
    var step = Number(m[1]);
    restoringRoute = true;
    if (document.getElementById("flow-view").hidden) {
      // Was on the dashboard, and pressing back landed on a setup step,
      // so actually leave the dashboard rather than just changing the
      // URL underneath it (same transition "Edit setup" performs).
      backToSetup(step);
    } else {
      goToStep(step);
    }
    restoringRoute = false;
  });

  // splash screen (once ever, remembered via localStorage)
  (function () {
    var SPLASH_KEY = "wattcast-splash-seen";
    if (localStorage.getItem(SPLASH_KEY)) {
      splashDone = true;
      maybeEnterApp();
      return;
    }
    localStorage.setItem(SPLASH_KEY, "1");

    var splash = document.getElementById("splash-view");
    splash.hidden = false;

    // Closes only when tapped, no auto-close timer.
    splash.addEventListener("click", function () {
      splash.classList.add("splash-exit");
      setTimeout(function () {
        splash.hidden = true;
        splashDone = true;
        maybeEnterApp();
      }, reduceMotion ? 0 : 400);
    });
  })();

  // theme toggle
  // Defaults to whatever the device's own light/dark setting is (the
  // CSS's own @media(prefers-color-scheme) rule already handles this
  // natively, no data-theme attribute needs stamping for that, and
  // it live-follows an OS theme change made while the page is open).
  // The toggle button is an explicit override: once used, that choice
  // is remembered and wins over the device setting from then on.
  var THEME_KEY = "wattcast-theme";
  var root = document.documentElement;
  var toggle = document.getElementById("theme-toggle");
  var darkMedia = window.matchMedia("(prefers-color-scheme: dark)");

  function getEffectiveTheme() {
    var explicit = root.getAttribute("data-theme");
    return explicit || (darkMedia.matches ? "dark" : "light");
  }
  function updateToggleIcon(t) {
    toggle.querySelector("use").setAttribute("href", t === "dark" ? "#i-sun" : "#i-moon");
  }
  function setTheme(t) {
    root.setAttribute("data-theme", t);
    localStorage.setItem(THEME_KEY, t);
    updateToggleIcon(t);
  }

  toggle.addEventListener("click", function () {
    setTheme(getEffectiveTheme() === "dark" ? "light" : "dark");
  });

  var savedTheme = localStorage.getItem(THEME_KEY);
  if (savedTheme) root.setAttribute("data-theme", savedTheme);
  updateToggleIcon(getEffectiveTheme());

  darkMedia.addEventListener("change", function () {
    if (!localStorage.getItem(THEME_KEY)) updateToggleIcon(getEffectiveTheme());
  });

  // setup flow (first-time wizard)
  // The real 233-panel catalog (19 manufacturers) served live from
  // /api/panels (backend/app.py), which already excludes the 2
  // AzurSpace research cells at the source query, since they're dropped
  // from training entirely and can't be forecast.
  var modelsByManufacturer = {};
  var manufacturers = [];

  var flow = { manufacturer: null, model: null, partId: null, arimaSupported: false, qty: 10, location: null };

  // in-progress wizard persistence
  // Separate from SYSTEM_KEY (a *completed* setup): this is whatever's
  // been filled in so far, so a refresh mid-wizard lands back on the
  // same step with the same answers instead of starting over at 1.
  var DRAFT_KEY = "wattcast-draft";
  var restoringRoute = false; // true while goToStep() is reacting to a hash change, not a user click

  function saveDraft(step) {
    localStorage.setItem(DRAFT_KEY, JSON.stringify({
      step: step,
      manufacturer: flow.manufacturer,
      model: flow.model,
      partId: flow.partId,
      modelWatts: flow.modelWatts,
      arimaSupported: flow.arimaSupported,
      qty: flow.qty,
      location: flow.location,
      latitude: flow.latitude,
      longitude: flow.longitude,
      rangeStart: toISODate(rangeStart),
      rangeEnd: toISODate(rangeEnd),
    }));
  }

  function loadDraft() {
    try {
      return JSON.parse(localStorage.getItem(DRAFT_KEY) || "null");
    } catch (e) {
      return null;
    }
  }

  function clearDraft() {
    localStorage.removeItem(DRAFT_KEY);
  }

  // The step also lives in the URL (#setup/N), not just localStorage.
  // That's what makes the browser's back/forward buttons move
  // between steps, and what refresh reads before anything else runs.
  function goToStep(n) {
    document.querySelectorAll(".flow-step").forEach(function (el) {
      el.classList.toggle("active", el.getAttribute("data-step") === String(n));
    });
    document.querySelectorAll(".flow-progress i").forEach(function (el) {
      el.classList.toggle("done", Number(el.getAttribute("data-step")) <= n);
    });
    if (!restoringRoute) location.hash = "setup/" + n;
    saveDraft(n);
    document.getElementById("topbar-reset-btn").hidden = n <= 1; // nothing to reset yet on step 1
  }

  // Escape hatch out of a setup that's gone wrong. Clears every step's
  // answers (not just the current one) and drops the in-progress draft,
  // rather than making someone click Back through all 5 steps by hand.
  function resetWizard() {
    flow.manufacturer = null;
    flow.model = null;
    flow.partId = null;
    flow.modelWatts = undefined;
    flow.arimaSupported = false;
    flow.qty = 10;
    flow.location = null;
    flow.latitude = undefined;
    flow.longitude = undefined;

    document.querySelectorAll("#manufacturer-chips .chip").forEach(function (c) { c.classList.remove("selected"); });
    mfrSearchInput.value = "";
    filterManufacturers("");
    document.getElementById("step1-next").disabled = true;

    document.getElementById("model-search").value = "";
    document.getElementById("model-list").innerHTML = "";
    document.getElementById("step2-next").disabled = true;

    updateQtyReadout();

    searchInput.value = "";
    resultsEl.hidden = true;
    document.getElementById("location-found").classList.remove("show");
    document.getElementById("location-error").hidden = true;

    setArimaAvailable(false);
    document.querySelectorAll(".engine-option").forEach(function (o) {
      o.classList.toggle("selected", o.dataset.engine === "random_forest");
    });

    var freshToday = new Date();
    rangeStart = new Date(freshToday.getFullYear(), freshToday.getMonth(), freshToday.getDate() + 1);
    rangeEnd = new Date(rangeStart.getFullYear(), rangeStart.getMonth(), rangeStart.getDate() + 2);
    pickingFresh = true;

    document.getElementById("wizard-error").hidden = true;
    document.getElementById("flow-view").hidden = false; // in case reset is clicked mid-fetch, while flow-view is momentarily hidden behind the loading view
    clearDraft();
    goToStep(1);
  }
  document.getElementById("topbar-reset-btn").addEventListener("click", resetWizard);

  var chipRow = document.getElementById("manufacturer-chips");
  var mfrSearchInput = document.getElementById("manufacturer-search");

  fetch("/api/panels")
    .then(function (res) { return res.json(); })
    .then(function (panels) {
      panels.forEach(function (p) {
        if (!modelsByManufacturer[p.manufacturer]) modelsByManufacturer[p.manufacturer] = [];
        modelsByManufacturer[p.manufacturer].push({
          n: p.model_name, w: p.pmp, partId: p.part_id, arimaSupported: p.arima_supported,
        });
      });
      manufacturers = Object.keys(modelsByManufacturer).sort();
      Object.keys(modelsByManufacturer).forEach(function (m) {
        modelsByManufacturer[m].sort(function (a, b) { return a.w - b.w; });
      });

      mfrSearchInput.placeholder = "Search " + manufacturers.length + " manufacturers";
      manufacturers.forEach(function (name) {
        var chip = document.createElement("div");
        chip.className = "chip";
        chip.textContent = name.replace(/_/g, " ");
        chip.dataset.name = name.toLowerCase();
        chip.addEventListener("click", function () {
          document.querySelectorAll("#manufacturer-chips .chip").forEach(function (c) { c.classList.remove("selected"); });
          chip.classList.add("selected");
          flow.manufacturer = name;
          flow.model = null;
          flow.partId = null;
          document.getElementById("step1-next").disabled = false;
        });
        chipRow.appendChild(chip);
      });

      panelsReady = true;
      savedSystem = loadSavedSystem();
      maybeEnterApp();
    })
    .catch(function () {
      // Without this, a failed fetch here means panelsReady never
      // becomes true, so maybeEnterApp() never proceeds. The user
      // would be stuck behind the splash screen forever with no
      // feedback at all, worse than a bad error message.
      document.getElementById("splash-view").hidden = true;
      document.getElementById("flow-view").hidden = false;
      var wizardError = document.getElementById("wizard-error");
      wizardError.textContent = "Couldn't load the panel catalog. Check your connection and reload the page.";
      wizardError.hidden = false;
    });

  function filterManufacturers(text) {
    var q = text.trim().toLowerCase();
    var visibleCount = 0;
    document.querySelectorAll("#manufacturer-chips .chip").forEach(function (chip) {
      var match = !q || chip.dataset.name.indexOf(q) !== -1;
      chip.hidden = !match;
      if (match) visibleCount++;
    });
    document.getElementById("manufacturer-empty").hidden = visibleCount > 0;
  }
  mfrSearchInput.addEventListener("input", function (e) { filterManufacturers(e.target.value); });

  document.getElementById("step1-next").addEventListener("click", function () {
    renderModelList("");
    goToStep(2);
  });

  function setArimaAvailable(available) {
    var opt = document.getElementById("engine-option-arima");
    opt.classList.toggle("disabled", !available);
    if (!available && opt.classList.contains("selected")) {
      opt.classList.remove("selected");
      document.querySelector('.engine-option[data-engine="random_forest"]').classList.add("selected");
    }
  }

  function renderModelList(filterText) {
    var list = document.getElementById("model-list");
    list.innerHTML = "";
    var models = (modelsByManufacturer[flow.manufacturer] || []).filter(function (m) {
      var q = filterText.trim().toLowerCase();
      if (!q) return true;
      return m.n.toLowerCase().indexOf(q) !== -1 || String(m.w).indexOf(q) !== -1;
    });
    document.getElementById("model-empty").hidden = models.length > 0;
    models.forEach(function (m) {
      var row = document.createElement("div");
      row.className = "model-row" + (flow.model === m.n ? " selected" : "");
      row.innerHTML =
        '<div><div class="name">' + m.n + '</div></div>' +
        '<div style="display:flex;align-items:center;">' +
        '<span class="watt">' + m.w.toFixed(1) + ' W</span>' +
        '<span class="check"><svg><use href="#i-check"/></svg></span>' +
        '</div>';
      row.addEventListener("click", function () {
        flow.model = m.n;
        flow.modelWatts = m.w;
        flow.partId = m.partId;
        flow.arimaSupported = m.arimaSupported;
        setArimaAvailable(m.arimaSupported);
        renderModelList(document.getElementById("model-search").value);
        document.getElementById("step2-next").disabled = false;
      });
      list.appendChild(row);
    });
  }
  document.getElementById("model-search").addEventListener("input", function (e) {
    renderModelList(e.target.value);
  });
  document.getElementById("step2-back").addEventListener("click", function () { goToStep(1); });
  document.getElementById("step2-next").addEventListener("click", function () {
    updateQtyReadout();
    goToStep(3);
  });

  function updateQtyReadout() {
    document.getElementById("qty-count").value = flow.qty;
    var kw = (flow.qty * (flow.modelWatts || 0)) / 1000;
    document.getElementById("qty-total-kw").textContent = kw.toFixed(1);
  }
  document.getElementById("qty-count").addEventListener("input", function (e) {
    var n = parseInt(e.target.value, 10);
    flow.qty = isNaN(n) ? 0 : n;
    var kw = (flow.qty * (flow.modelWatts || 0)) / 1000;
    document.getElementById("qty-total-kw").textContent = kw.toFixed(1);
  });
  document.getElementById("qty-count").addEventListener("blur", function () {
    flow.qty = Math.max(1, flow.qty || 1);
    updateQtyReadout();
  });
  document.getElementById("qty-minus").addEventListener("click", function () {
    flow.qty = Math.max(1, flow.qty - 1);
    updateQtyReadout();
  });
  document.getElementById("qty-plus").addEventListener("click", function () {
    flow.qty = flow.qty + 1;
    updateQtyReadout();
  });
  document.getElementById("step3-back").addEventListener("click", function () { goToStep(2); });
  document.getElementById("step3-next").addEventListener("click", function () { goToStep(4); });

  function showLocationFound(label) {
    document.getElementById("location-error").hidden = true;
    document.getElementById("location-found-text").textContent = "Using: " + label;
    document.getElementById("location-found").classList.add("show");
  }

  function showLocationError(message) {
    document.getElementById("location-found").classList.remove("show");
    var el = document.getElementById("location-error");
    el.textContent = message;
    el.hidden = false;
  }

  function resolvePosition(pos) {
    var btn = document.getElementById("locate-btn");
    flow.latitude = pos.coords.latitude;
    flow.longitude = pos.coords.longitude;
    flow.location = flow.latitude.toFixed(2) + ", " + flow.longitude.toFixed(2); // fallback if reverse geocoding below fails

    // Turns the raw coordinates into a real place name: free,
    // keyless, browser-callable reverse geocoding (no server round
    // trip needed just to label a point on the map).
    fetch("https://api.bigdatacloud.net/data/reverse-geocode-client?latitude=" + flow.latitude + "&longitude=" + flow.longitude + "&localityLanguage=en")
      .then(function (res) { return res.json(); })
      .then(function (place) {
        var city = place.city || place.locality;
        if (city) flow.location = city + (place.countryName ? ", " + place.countryName : "");
      })
      .catch(function () { /* keep the coordinate fallback already set above */ })
      .then(function () {
        showLocationFound(flow.location);
        btn.classList.remove("detecting");
        btn.lastChild.textContent = " Use my current location";
      });
  }

  // code 2 (POSITION_UNAVAILABLE) is what macOS/Safari's CoreLocation
  // reports as kCLErrorLocationUnknown. Apple's own docs describe
  // this as usually transient (it couldn't get a fix *this instant*,
  // not that it never will), so one quick automatic retry before
  // treating it as a real failure clears most of these on its own.
  function requestLocation(retried) {
    var btn = document.getElementById("locate-btn");
    navigator.geolocation.getCurrentPosition(
      resolvePosition,
      function (err) {
        if (err.code === 2 && !retried) {
          setTimeout(function () { requestLocation(true); }, 600);
          return;
        }
        btn.classList.remove("detecting");
        btn.lastChild.textContent = " Use my current location";
        showLocationError(
          err.code === 1
            ? "Location access was denied. Enter a city below, or continue to use the default site."
            : "Couldn't determine your location. Enter a city below, or continue to use the default site."
        );
      },
      { timeout: 10000, maximumAge: 60000 }
    );
  }

  document.getElementById("locate-btn").addEventListener("click", function (e) {
    var btn = e.currentTarget;

    if (!navigator.geolocation) {
      showLocationError("Location isn't available in this browser. Enter a city below, or continue to use the default site.");
      return;
    }

    document.getElementById("location-error").hidden = true;
    btn.classList.add("detecting");
    btn.lastChild.textContent = " Locating…";
    requestLocation(false);
  });

  // Real forward geocoding: Open-Meteo's own geocoding API (the
  // same provider this project already depends on for weather), so
  // typing a city resolves to real coordinates, not just a label.
  var geocodeDebounce = null;
  var searchInput = document.getElementById("location-search");
  var resultsEl = document.getElementById("geocode-results");

  searchInput.addEventListener("input", function (e) {
    var query = e.target.value.trim();
    clearTimeout(geocodeDebounce);
    if (query.length < 2) {
      resultsEl.hidden = true;
      return;
    }
    geocodeDebounce = setTimeout(function () {
      fetch("https://geocoding-api.open-meteo.com/v1/search?count=6&language=en&format=json&name=" + encodeURIComponent(query))
        .then(function (res) { return res.json(); })
        .then(function (data) {
          var results = data.results || [];
          resultsEl.innerHTML = "";
          results.forEach(function (r) {
            var detail = [r.admin1, r.country].filter(Boolean).join(", ");
            var row = document.createElement("div");
            row.className = "geocode-row";
            row.innerHTML = '<div class="place-name"></div>' + (detail ? '<div class="place-detail"></div>' : "");
            row.querySelector(".place-name").textContent = r.name;
            if (detail) row.querySelector(".place-detail").textContent = detail;
            row.addEventListener("click", function () {
              flow.latitude = r.latitude;
              flow.longitude = r.longitude;
              flow.location = r.name + (detail ? ", " + detail : "");
              searchInput.value = flow.location;
              resultsEl.hidden = true;
              showLocationFound(flow.location);
            });
            resultsEl.appendChild(row);
          });
          resultsEl.hidden = results.length === 0;
        })
        .catch(function () { resultsEl.hidden = true; });
    }, 350);
  });
  document.addEventListener("click", function (e) {
    if (!resultsEl.hidden && e.target !== searchInput && !resultsEl.contains(e.target)) {
      resultsEl.hidden = true;
    }
  });
  document.getElementById("step4-back").addEventListener("click", function () { goToStep(3); });
  document.getElementById("step4-next").addEventListener("click", function () {
    renderDayStrip("wizard-day-strip", selectDateFromWizard);
    goToStep(5);
  });
  document.getElementById("step5-back").addEventListener("click", function () { goToStep(4); });
  document.getElementById("step5-next").addEventListener("click", function () {
    showLoadingScreen(revealDashboard);
  });

  // Mirrors what a real backend actually does in sequence (fetch live
  // weather, then run the chosen model) rather than a generic spinner
  // with no explanation (see common/inference.py's predict_with_weather()).
  // Shows the loading screen, kicks off the real forecast fetch (see
  // fetchAndRenderForecast()), and only reveals the dashboard once it
  // actually resolves. The two status messages are timed for pacing,
  // but advancing past the loading screen always waits on the real
  // response, success or failure.
  function showLoadingScreen(onSuccess) {
    var engineEl = document.querySelector(".engine-option.selected");
    var engineValue = engineEl ? engineEl.dataset.engine : "random_forest";
    var nameEl = engineEl ? engineEl.querySelector(".name") : null;
    var engine = nameEl && nameEl.firstChild ? nameEl.firstChild.textContent.trim() : "Random Forest";

    // Nothing about this system/range/engine has actually changed
    // since the last successful forecast, so skip the fetch AND the
    // loading screen itself. Showing a fake "fetching weather" moment
    // for data that's just being read back out of localStorage would
    // be its own kind of waste.
    if (getFreshCachedForecast(engineValue)) {
      fetchAndRenderForecast(false).then(onSuccess);
      return;
    }

    var loc = flow.location || "the default site";
    var flowView = document.getElementById("flow-view");
    var loadingView = document.getElementById("loading-view");
    var status = document.getElementById("loading-status");
    var wizardError = document.getElementById("wizard-error");

    wizardError.hidden = true;
    flowView.hidden = true;
    loadingView.hidden = false;
    status.textContent = "Fetching live weather for " + loc + "…";

    var messageTimer1 = setTimeout(function () {
      status.textContent = "Running " + engine + " inference…";
    }, 700);
    // The ML prediction above is normally done well before this fires;
    // if it hasn't, the real bottleneck is almost always the live
    // Simscape simulation running concurrently with it (a cold MATLAB
    // engine start alone takes ~25-30s, see common/simulation.py), so
    // say so rather than leaving the same inference message sitting
    // there looking stuck.
    var messageTimer2 = setTimeout(function () {
      status.textContent = "Comparing against a live simulation, this can take a minute the first time…";
    }, 4000);

    fetchAndRenderForecast(false)
      .then(function () {
        clearTimeout(messageTimer1);
        clearTimeout(messageTimer2);
        loadingView.hidden = true;
        onSuccess();
      })
      .catch(function (err) {
        clearTimeout(messageTimer1);
        clearTimeout(messageTimer2);
        loadingView.hidden = true;
        flowView.hidden = false;
        wizardError.textContent = err.message;
        wizardError.hidden = false;
      });
  }

  // Real location + real freshness, shown only once there's an actual
  // forecast to describe. Meaningless during setup, where neither
  // exists yet, which is why it starts hidden and only the dashboard
  // ever reveals it.
  var lastUpdatedAt = null;
  var clockInterval = null;

  function formatRelativeTime(date) {
    var seconds = Math.floor((Date.now() - date.getTime()) / 1000);
    if (seconds < 60) return "just now";
    var minutes = Math.floor(seconds / 60);
    if (minutes < 60) return minutes + " min ago";
    var hours = Math.floor(minutes / 60);
    return hours + " hr ago";
  }

  function updateClockContext() {
    if (!lastUpdatedAt) return;
    var loc = flow.location || "the default site";
    document.getElementById("clock-context").textContent = loc + " · Updated " + formatRelativeTime(lastUpdatedAt);
  }

  // atDate is when the data actually came from the server, which
  // isn't always "now": a cache hit should show its real (older)
  // fetch time, not claim to be fresher than it is.
  function showClockContext(atDate) {
    lastUpdatedAt = atDate;
    document.getElementById("clock-context").hidden = false;
    updateClockContext();
    if (!clockInterval) clockInterval = setInterval(updateClockContext, 30000);
  }

  function revealDashboard() {
    saveSystem();
    clearDraft(); // setup finished, SYSTEM_KEY is now the source of truth, not the in-progress draft
    if (!restoringRoute) location.hash = "dashboard";
    var dashView = document.getElementById("dashboard-view");
    dashView.style.display = "";
    dashView.classList.remove("fade-enter");
    void dashView.offsetWidth;
    dashView.classList.add("fade-enter");
    document.getElementById("edit-setup-btn").hidden = false;
    document.getElementById("topbar-performance-btn").hidden = false;
    document.getElementById("topbar-reset-btn").hidden = true;

    var panelLabel = (flow.manufacturer && flow.model) ? (flow.manufacturer + " " + flow.model) : "";
    var qty = flow.qty || 10;
    var loc = flow.location || "the default site";
    document.querySelectorAll(".field-system").forEach(function (el) {
      el.querySelector(".field-value").lastChild.textContent = " " + panelLabel;
    });
    var qtyField = document.querySelector(".field-qty input");
    if (qtyField) qtyField.value = qty;
    document.querySelectorAll(".field-location .field-value").forEach(function (el) {
      el.lastChild.textContent = " " + loc;
    });
    document.getElementById("date-field-text").textContent = formatRangeShort(rangeStart, rangeEnd);
  }

  function backToSetup(step) {
    document.getElementById("dashboard-view").style.display = "none";
    document.getElementById("flow-view").hidden = false;
    document.getElementById("edit-setup-btn").hidden = true;
    document.getElementById("topbar-performance-btn").hidden = true;
    document.getElementById("clock-context").hidden = true;
    // topbar-reset-btn's visibility is step-driven, goToStep() below sets it
    renderDayStrip("wizard-day-strip", selectDateFromWizard);
    goToStep(step);
  }

  document.getElementById("edit-setup-btn").addEventListener("click", function () {
    backToSetup(5);
  });

  // advanced disclosure
  var advToggle = document.getElementById("advanced-toggle");
  var advPanel = document.getElementById("advanced-panel");
  advToggle.addEventListener("click", function () {
    advPanel.classList.toggle("open");
    advToggle.classList.toggle("open");
  });
  document.querySelectorAll(".engine-option").forEach(function (opt) {
    opt.addEventListener("click", function () {
      document.querySelectorAll(".engine-option").forEach(function (o) { o.classList.remove("selected"); });
      opt.classList.add("selected");
    });
  });

  // forecast date range picker (today through +14 days, the
  // live weather forecast's real reach)
  var DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  var MONTH = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  var today0 = new Date();
  var rangeStart = new Date(today0.getFullYear(), today0.getMonth(), today0.getDate() + 1); // default to tomorrow, matching the CLI's default
  var rangeEnd = new Date(rangeStart.getFullYear(), rangeStart.getMonth(), rangeStart.getDate() + 2); // 3-day default span
  var pickingFresh = true; // true = next click starts a brand-new range rather than completing this one

  function formatDateShort(d) { return DOW[d.getDay()] + ", " + MONTH[d.getMonth()] + " " + d.getDate(); }
  function formatRangeShort(start, end) {
    return start.toDateString() === end.toDateString()
      ? formatDateShort(start)
      : formatDateShort(start) + " to " + formatDateShort(end);
  }

  // Standard 2-click range picker: first click starts a fresh
  // single-day range, second click extends it into an end date
  // (or, if clicked before the start, restarts the range there).
  function applyRangeClick(d) {
    if (pickingFresh) {
      rangeStart = d; rangeEnd = d; pickingFresh = false;
    } else if (d < rangeStart) {
      rangeStart = d; rangeEnd = d;
    } else {
      rangeEnd = d; pickingFresh = true;
    }
  }

  // Shared by both the dashboard's date popover and step 5 of the
  // wizard. containerId picks where the strip renders, onSelect
  // picks what happens after a day is clicked (the wizard just
  // re-renders itself and its summary line; the dashboard also
  // updates the field text and only closes once a full range exists).
  function renderDayStrip(containerId, onSelect) {
    var strip = document.getElementById(containerId);
    strip.innerHTML = "";
    var today = new Date();
    for (var i = 0; i <= 14; i++) {
      // Normalized to midnight, deliberately: comparing against
      // rangeStart/rangeEnd (also always midnight-normalized, see
      // below) has to be exact, not "close enough": a Date carrying
      // whatever time-of-day the script happened to run at would
      // make same-day comparisons unreliable by a few milliseconds.
      var d = new Date(today.getFullYear(), today.getMonth(), today.getDate() + i);
      var isEndpoint = d.getTime() === rangeStart.getTime() || d.getTime() === rangeEnd.getTime();
      var isInRange = d > rangeStart && d < rangeEnd;
      var chip = document.createElement("div");
      chip.className = "day-chip" + (isEndpoint ? " selected" : "") + (isInRange ? " in-range" : "");
      var dowLabel = i === 0 ? "Today" : (i === 1 ? "Tmrw" : DOW[d.getDay()]);
      chip.innerHTML = '<span class="dow">' + dowLabel + '</span><span class="dom">' + d.getDate() + '</span>';
      (function (d) {
        chip.addEventListener("click", function () { onSelect(d); });
      })(d);
      strip.appendChild(chip);
    }
  }

  function updateWizardSummary() {
    var n = dayCount(rangeStart, rangeEnd);
    document.getElementById("wizard-range-summary").textContent =
      (n === 1 ? "1 day selected: " : n + " days selected: ") + formatRangeShort(rangeStart, rangeEnd);
  }

  function selectDateFromWizard(d) {
    applyRangeClick(d);
    renderDayStrip("wizard-day-strip", selectDateFromWizard);
    updateWizardSummary();
  }

  function selectDateFromDashboard(d) {
    applyRangeClick(d);
    renderDayStrip("day-strip", selectDateFromDashboard);
    document.getElementById("date-field-text").textContent = formatRangeShort(rangeStart, rangeEnd);
    if (pickingFresh) {
      document.getElementById("date-popover").hidden = true; // range just completed
      var dashboardError = document.getElementById("dashboard-error");
      dashboardError.hidden = true;
      document.getElementById("dashboard-loading-overlay").hidden = false;
      fetchAndRenderForecast(true) // the range itself just changed, always a real miss
        .then(function () {
          saveSystem();
        })
        .catch(function (err) {
          dashboardError.textContent = err.message;
          dashboardError.hidden = false;
        })
        .then(function () {
          document.getElementById("dashboard-loading-overlay").hidden = true;
        });
    }
  }

  document.getElementById("date-field-text").textContent = formatRangeShort(rangeStart, rangeEnd);
  var datePopover = document.getElementById("date-popover");
  document.getElementById("date-field-btn").addEventListener("click", function (e) {
    e.stopPropagation();
    if (datePopover.hidden) renderDayStrip("day-strip", selectDateFromDashboard);
    datePopover.hidden = !datePopover.hidden;
  });
  document.addEventListener("click", function (e) {
    if (!datePopover.hidden && !document.getElementById("date-field-wrap").contains(e.target)) {
      datePopover.hidden = true;
    }
  });

  // update forecast
  document.getElementById("dashboard-qty-input").addEventListener("input", function (e) {
    var n = parseInt(e.target.value, 10);
    if (!isNaN(n) && n > 0) flow.qty = n;
  });

  document.getElementById("update-forecast-btn").addEventListener("click", function () {
    var btn = this;
    var original = btn.textContent;
    var dashboardError = document.getElementById("dashboard-error");
    dashboardError.hidden = true;
    btn.classList.add("loading");
    btn.textContent = "Updating…";
    btn.disabled = true;
    document.getElementById("dashboard-loading-overlay").hidden = false;

    fetchAndRenderForecast(true) // explicit request, always bypasses the cache
      .then(function () {
        saveSystem();
        var hero = document.querySelector(".hero");
        hero.classList.remove("fade-enter");
        void hero.offsetWidth;
        hero.classList.add("fade-enter");
      })
      .catch(function (err) {
        dashboardError.textContent = err.message;
        dashboardError.hidden = false;
      })
      .then(function () {
        btn.classList.remove("loading");
        btn.textContent = original;
        btn.disabled = false;
        document.getElementById("dashboard-loading-overlay").hidden = true;
      });
  });

  // model performance modal
  // Static per model (only changes if a model is retrained), so this
  // loads once at page load rather than per forecast (see
  // _load_model_performance() in backend/app.py, which reads the real
  // output/tables/*.csv evaluation results).
  fetch("/api/model-performance")
    .then(function (res) { return res.json(); })
    .then(function (perf) {
      function fill(prefix, m) {
        // toFixed(5), not 4: Random Forest's real R2 (0.9999935870843596)
        // rounds to a misleading "1.0000" at 4 decimals, which reads as a
        // suspiciously perfect fit instead of the genuine value.
        document.getElementById(prefix + "-r2").textContent = m.R2.toFixed(5);
        document.getElementById(prefix + "-mae").textContent = m.MAE.toFixed(2) + " W";
        document.getElementById(prefix + "-rmse").textContent = m.RMSE.toFixed(2) + " W";
        document.getElementById(prefix + "-mape").textContent = m.MAPE.toFixed(1) + "%";
      }
      fill("perf-rf", perf.random_forest);
      fill("perf-lstm", perf.lstm);
      fill("perf-arima", perf.arima);
    })
    .catch(function () {
      ["perf-rf", "perf-lstm", "perf-arima"].forEach(function (prefix) {
        ["r2", "mae", "rmse", "mape"].forEach(function (metric) {
          document.getElementById(prefix + "-" + metric).textContent = "N/A";
        });
      });
    });

  var perfModal = document.getElementById("performance-modal");
  function openPerfModal() { perfModal.hidden = false; }
  function closePerfModal() { perfModal.hidden = true; }
  document.getElementById("topbar-performance-btn").addEventListener("click", openPerfModal);
  document.getElementById("close-performance-btn").addEventListener("click", closePerfModal);
  perfModal.addEventListener("click", function (e) {
    if (e.target === perfModal) closePerfModal();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { closePerfModal(); datePopover.hidden = true; }
  });

  // smooth path helper (Catmull-Rom -> cubic bezier)
  function smoothLine(points) {
    if (points.length < 2) return "";
    var d = "M " + points[0].x + "," + points[0].y;
    for (var i = 0; i < points.length - 1; i++) {
      var p0 = points[i - 1] || points[i];
      var p1 = points[i];
      var p2 = points[i + 1];
      var p3 = points[i + 2] || p2;
      var c1x = p1.x + (p2.x - p0.x) / 6;
      var c1y = p1.y + (p2.y - p0.y) / 6;
      var c2x = p2.x - (p3.x - p1.x) / 6;
      var c2y = p2.y - (p3.y - p1.y) / 6;
      d += " C " + c1x + "," + c1y + " " + c2x + "," + c2y + " " + p2.x + "," + p2.y;
    }
    return d;
  }

  function toPoints(values, w, h, padTop, minVal, maxVal) {
    var n = values.length;
    return values.map(function (v, i) {
      var x = (i / (n - 1)) * w;
      var y = padTop + (1 - (v - minVal) / (maxVal - minVal)) * (h - padTop);
      return { x: x, y: y };
    });
  }

  // detail chart builder (power / irradiance / temperature)
  // vals spans however many days are selected (24 * numDays entries),
  // rendered as one continuous line. Day boundaries get a faint
  // divider on every chart, and (only on the bottom, shared-axis
  // chart) a date label, thinned out for longer ranges so a 14-day
  // range doesn't cram 14 labels into a fixed-width chart.
  function buildMetric(container, opts) {
    var w = 680, h = 150, padTop = 10, padBottom = opts.showAxis ? 24 : 6, padLeft = 34;
    var plotH = h - padTop - padBottom;
    var plotW = w - padLeft - 8;
    var vals = opts.values;
    var numDays = vals.length / 24;
    var rawMax = Math.max.apply(null, vals);
    var rawMin = opts.zeroBased ? 0 : Math.min.apply(null, vals);
    if (opts.ref) rawMax = Math.max(rawMax, opts.ref);
    var min = opts.zeroBased ? 0 : rawMin - (rawMax - rawMin) * 0.15;
    var max = rawMax + (rawMax - (opts.zeroBased ? 0 : rawMin)) * 0.12;
    if (max === min) max = min + 1;

    function X(i) { return padLeft + (i / (vals.length - 1)) * plotW; }
    function Y(v) { return padTop + (1 - (v - min) / (max - min)) * plotH; }

    var pts = vals.map(function (v, i) { return { x: X(i), y: Y(v) }; });
    var line = smoothLine(pts);

    var svgParts = [];
    var ticks = 3;
    for (var t = 0; t <= ticks; t++) {
      var val = min + ((max - min) * t) / ticks;
      var y = Y(val);
      svgParts.push('<line class="metric-grid" x1="' + padLeft + '" x2="' + w + '" y1="' + y + '" y2="' + y + '"/>');
      svgParts.push('<text class="metric-tick" x="' + (padLeft - 6) + '" y="' + (y + 3) + '" text-anchor="end">' + Math.round(val) + '</text>');
    }
    for (var dv = 1; dv < numDays; dv++) {
      var dx = X(dv * 24);
      svgParts.push('<line class="metric-day-divider" x1="' + dx + '" x2="' + dx + '" y1="' + padTop + '" y2="' + (padTop + plotH) + '"/>');
    }
    // Hour-level ticks, not just one day-name label per day: a step
    // chosen to keep roughly 6-8 ticks total whether this is a single
    // day or a full 14-day range. Midnight ticks show the date (since
    // that's the one moment worth naming the day at all); every other
    // tick just shows the time, so reading across the comparison line
    // below doesn't require counting hours by eye.
    if (opts.showAxis && opts.timestamps) {
      var stepOptions = [1, 2, 3, 4, 6, 8, 12, 24, 48, 72];
      var hourStep = stepOptions[stepOptions.length - 1];
      for (var s = 0; s < stepOptions.length; s++) {
        if (Math.ceil(vals.length / stepOptions[s]) <= 8) { hourStep = stepOptions[s]; break; }
      }
      for (var hh = 0; hh < vals.length; hh += hourStep) {
        var tickX = X(hh);
        var tickT = opts.timestamps[hh];
        var tickLabel = tickT.getHours() === 0
          ? MONTH[tickT.getMonth()] + " " + tickT.getDate()
          : String(tickT.getHours()).padStart(2, "0") + ":00";
        svgParts.push('<text class="metric-tick" x="' + tickX + '" y="' + (h - 6) + '" text-anchor="middle">' + tickLabel + '</text>');
      }
    }
    if (opts.ref) {
      var ry = Y(opts.ref);
      svgParts.push('<line class="metric-ref-line" x1="' + padLeft + '" x2="' + w + '" y1="' + ry + '" y2="' + ry + '"/>');
    }
    svgParts.push('<path class="metric-line" d="' + line + '" stroke="' + opts.color + '"/>');
    if (opts.compareValues) {
      // Small dots at each real hourly point, not a continuous stroke:
      // a stroke wide enough to guarantee visibility when the two
      // series are pixel-identical (which they often are here) reads
      // as a thick, muddy halo. Dots drawn on top stay compact and
      // legible either way, sitting quietly on the line where the
      // values agree and standing off it exactly where they don't.
      var compareDots = opts.compareValues.map(function (v, i) {
        return '<circle class="metric-compare-dot" cx="' + X(i) + '" cy="' + Y(v) + '" r="2"/>';
      }).join("");
      svgParts.push('<g fill="' + opts.compareColor + '">' + compareDots + '</g>');
    }
    if (opts.annotatePeak) {
      var peakIdx = vals.indexOf(Math.max.apply(null, vals));
      svgParts.push('<circle class="metric-peak" cx="' + pts[peakIdx].x + '" cy="' + pts[peakIdx].y + '" r="4" stroke="' + opts.color + '"/>');
      svgParts.push('<text class="metric-peak-label" x="' + pts[peakIdx].x + '" y="' + (pts[peakIdx].y - 10) + '" text-anchor="middle" fill="var(--ink)">' + opts.peakLabel + '</text>');
    }

    var refBits = [];
    if (opts.ref) refBits.push('<span>' + opts.refLabel + '</span>');
    if (opts.compareValues) {
      refBits.push(
        '<span class="metric-compare-label"><span class="swatch swatch-dotted" style="border-color:' +
        opts.compareColor + '"></span>' + opts.compareLabel + '</span>'
      );
    }

    var block = document.createElement("div");
    block.className = "metric-block";
    block.innerHTML =
      '<div class="metric-head">' +
        '<div class="metric-title"><span class="swatch" style="background:' + opts.color + '"></span>' + opts.title + '</div>' +
        (refBits.length ? '<div class="metric-ref">' + refBits.join(" &middot; ") + '</div>' : '') +
      '</div>' +
      '<svg viewBox="0 0 ' + w + ' ' + h + '">' + svgParts.join("") + '</svg>';
    container.appendChild(block);
  }

  // forecast result cache
  // A page reload with an unchanged setup shouldn't re-fetch weather
  // and re-run model inference just to show the exact same numbers
  // again, since that's real, wasted work on every visit. Cached by the
  // full set of request parameters (panel, range, quantity, location,
  // engine): any of those changing is a real cache miss, not a stale
  // hit. TTL exists because weather itself changes, so an hour-old
  // "current" forecast isn't actually current anymore.
  var FORECAST_CACHE_KEY = "wattcast-forecast-cache";
  var FORECAST_CACHE_TTL_MS = 15 * 60 * 1000;

  function currentForecastParamsKey(model) {
    return JSON.stringify({
      partId: flow.partId,
      startDate: toISODate(rangeStart),
      endDate: toISODate(rangeEnd),
      model: model,
      qty: flow.qty || 10,
      lat: flow.latitude != null ? flow.latitude : null,
      lon: flow.longitude != null ? flow.longitude : null,
    });
  }

  // Returns the full {data, cachedAt} record, not just the data:
  // callers need the real fetch time to show honest "Updated X ago"
  // freshness, not the moment they happened to read the cache.
  function getFreshCachedForecast(model) {
    var cached;
    try {
      cached = JSON.parse(localStorage.getItem(FORECAST_CACHE_KEY) || "null");
    } catch (e) {
      return null;
    }
    if (!cached) return null;
    if (cached.paramsKey !== currentForecastParamsKey(model)) return null;
    if (Date.now() - cached.cachedAt >= FORECAST_CACHE_TTL_MS) return null;
    return cached;
  }

  function saveForecastCache(model, data) {
    localStorage.setItem(FORECAST_CACHE_KEY, JSON.stringify({
      paramsKey: currentForecastParamsKey(model),
      data: data,
      cachedAt: Date.now(),
    }));
  }

  // Every caller below shows err.message straight in the UI on a
  // rejection, so every rejection reaching them has to already be a
  // clean, written-for-humans message. fetch() itself rejecting
  // (offline, DNS failure, server unreachable) throws a raw TypeError
  // like "Failed to fetch", and a malformed response body throws a
  // raw JSON SyntaxError. Both get caught here and replaced before
  // they can leak through, for both the job-start and poll requests.
  function fetchJson(fetchPromise) {
    return fetchPromise
      .catch(function () {
        throw new Error("Couldn't reach the server. Check your connection and try again.");
      })
      .then(function (res) {
        return res.json()
          .catch(function () {
            throw new Error("Received an unreadable response from the server. Please try again.");
          })
          .then(function (data) {
            if (!res.ok) throw new Error(data.error || "Something went wrong.");
            return data;
          });
      });
  }

  // Fetches a real forecast via backend/app.py's /api/forecast/jobs:
  // one request kicks off the ML prediction and the live Simscape
  // simulation together, running concurrently, and this polls until
  // both are ready rather than holding one long request open the
  // whole time. Called after finishing setup, after the dashboard's
  // date popover completes a range, and on Update forecast.
  // forceRefresh skips the cache entirely: Update forecast always
  // passes true, since asking for an update should always ask.
  function fetchAndRenderForecast(forceRefresh) {
    var engineEl = document.querySelector(".engine-option.selected");
    var model = engineEl ? engineEl.dataset.engine : "random_forest";

    if (!forceRefresh) {
      var cached = getFreshCachedForecast(model);
      if (cached) {
        renderForecast(cached.data);
        showClockContext(new Date(cached.cachedAt));
        return Promise.resolve(cached.data);
      }
    }

    var params = new URLSearchParams({
      part_id: flow.partId,
      start_date: toISODate(rangeStart),
      end_date: toISODate(rangeEnd),
      model: model,
      quantity: String(flow.qty || 10),
    });
    if (flow.latitude != null && flow.longitude != null) {
      params.set("lat", flow.latitude);
      params.set("lon", flow.longitude);
    }

    return fetchJson(fetch("/api/forecast/jobs?" + params.toString(), { method: "POST" }))
      .then(function (started) {
        return new Promise(function (resolve, reject) {
          function poll() {
            fetchJson(fetch("/api/forecast/jobs/" + started.job_id))
              .then(function (data) {
                if (data.status === "pending") {
                  setTimeout(poll, 900);
                  return;
                }
                resolve(data);
              })
              .catch(reject);
          }
          poll();
        });
      })
      .then(function (data) {
        renderForecast(data);
        saveForecastCache(model, data);
        showClockContext(new Date());
        return data;
      });
  }

  function renderForecast(data) {
    var power = data.power.map(function (p) { return p.watts; });
    var timestamps = data.power.map(function (p) { return new Date(p.time); });
    var hasWeather = !!data.weather;
    var irradiance = hasWeather ? data.weather.map(function (w) { return w.irradiance_wm2; }) : null;
    var temperature = hasWeather ? data.weather.map(function (w) { return w.temperature_c; }) : null;
    var ratedTotal = data.rated_total_watts;
    var stc = data.stc_irradiance_wm2;
    var numDays = Math.round(power.length / 24);

    var dayLabels = [];
    for (var d = 0; d < numDays; d++) {
      var t0 = timestamps[d * 24];
      dayLabels.push(MONTH[t0.getMonth()] + " " + t0.getDate());
    }

    var totalKwh = power.reduce(function (a, b) { return a + b; }, 0) / 1000;
    var peakIdx = power.indexOf(Math.max.apply(null, power));
    var peakVal = power[peakIdx];
    var peakTime = timestamps[peakIdx];
    var qty = data.quantity;
    var panelLabel = data.specs.manufacturer + " " + data.specs.model_name;

    // hero text
    document.getElementById("hero-eyebrow").textContent = "Forecast for " + formatRangeShort(rangeStart, rangeEnd);
    document.getElementById("hero-insight-text").textContent = numDays === 1
      ? "A strong solar day, reaching " + Math.round(peakVal / ratedTotal * 100) + "% of your system's peak capacity"
      : "A strong " + numDays + "-day stretch, peaking at " + Math.round(peakVal).toLocaleString() + " W on " + DOW[peakTime.getDay()];
    var weatherBlurb;
    if (!hasWeather) {
      weatherBlurb = "trend-based forecast, no weather used";
    } else if (numDays === 1) {
      weatherBlurb = "high of " + Math.round(Math.max.apply(null, temperature)) + "&deg;C";
    } else {
      weatherBlurb = numDays + "-day outlook";
    }
    document.getElementById("hero-context").innerHTML = panelLabel + " &times; " + qty + " &middot; " + weatherBlurb;

    // hero number count-up
    (function () {
      var el = document.getElementById("hero-num");
      if (reduceMotion) { el.textContent = totalKwh.toFixed(1); return; }
      var start = null, duration = 1000;
      function tick(ts) {
        if (!start) start = ts;
        var p = Math.min(1, (ts - start) / duration);
        var eased = 1 - Math.pow(1 - p, 3);
        el.textContent = (totalKwh * eased).toFixed(1);
        if (p < 1) requestAnimationFrame(tick);
      }
      requestAnimationFrame(tick);
    })();

    // hero day-arc (continuous across every selected day)
    (function () {
      var w = 460, h = 170, padTop = 14, base = 148;
      var pts = toPoints(power, w, base - padTop, 0, 0, Math.max.apply(null, power) * 1.08);
      pts = pts.map(function (p) { return { x: p.x, y: p.y + padTop }; });
      var line = smoothLine(pts);
      var area = line + " L " + pts[pts.length - 1].x + "," + base + " L " + pts[0].x + "," + base + " Z";

      var svgParts = [
        '<defs><linearGradient id="heroFill" x1="0" y1="0" x2="0" y2="1">' +
        '<stop offset="0" stop-color="var(--hero-ink)" stop-opacity="0.22"/>' +
        '<stop offset="1" stop-color="var(--hero-ink)" stop-opacity="0"/></linearGradient></defs>',
        '<line class="arc-axis" x1="0" y1="' + base + '" x2="' + w + '" y2="' + base + '"/>'
      ];
      for (var dv = 1; dv < numDays; dv++) {
        var dx = (dv * 24 / (power.length - 1)) * w;
        svgParts.push('<line class="arc-day-divider" x1="' + dx + '" x2="' + dx + '" y1="' + padTop + '" y2="' + base + '"/>');
      }
      var labelStep = numDays <= 5 ? 1 : (numDays <= 10 ? 2 : 3);
      for (var dd = 0; dd < numDays; dd += labelStep) {
        var midIdx = Math.min(dd * 24 + 12, power.length - 1);
        var midX = (midIdx / (power.length - 1)) * w;
        svgParts.push('<text class="arc-label" x="' + midX + '" y="' + (base + 16) + '" text-anchor="middle">' + dayLabels[dd] + '</text>');
      }
      svgParts.push('<path class="arc-fill fade-in" style="animation-delay:.9s" d="' + area + '" fill="url(#heroFill)"/>');
      svgParts.push('<path class="arc-line" id="arc-line-path" d="' + line + '"/>');
      svgParts.push('<circle class="arc-peak-dot fade-in" style="animation-delay:1.1s" cx="' + pts[peakIdx].x + '" cy="' + pts[peakIdx].y + '" r="4"/>');

      var svg = document.getElementById("hero-arc");
      svg.innerHTML = svgParts.join("");

      var path = document.getElementById("arc-line-path");
      var len = path.getTotalLength();
      path.style.setProperty("--len", len);
      path.classList.add("draw-in");
      if (reduceMotion) { path.style.animation = "none"; path.style.strokeDashoffset = 0; }
    })();

    // detail charts. ARIMA never sees weather (data.weather is null),
    // so only the power chart renders. The power chart always gets its
    // own time/date axis now (not just when it's the only chart in the
    // stack), since that's the one showing the simulated comparison
    // line and reading exactly when it diverges needs real ticks, not
    // just whichever chart happens to be at the bottom.
    var hasSimulated = !!data.simulated;
    var simulatedValues = hasSimulated ? data.simulated.map(function (s) { return s.watts; }) : null;

    var detailContainer = document.getElementById("detail-charts");
    detailContainer.innerHTML = "";
    buildMetric(detailContainer, {
      title: "Power (W)", color: "var(--sun-strong)", values: power, zeroBased: true,
      ref: ratedTotal, refLabel: "Rated capacity &middot; " + ratedTotal.toLocaleString() + " W",
      annotatePeak: true, peakLabel: Math.round(peakVal).toLocaleString() + " W",
      compareValues: simulatedValues, compareColor: "var(--ink)", compareLabel: "Simulated (ground truth)",
      showAxis: true, timestamps: timestamps
    });
    if (hasWeather) {
      buildMetric(detailContainer, {
        title: "Irradiance (W/m&sup2;)", color: "var(--sky)", values: irradiance, zeroBased: true,
        ref: stc, refLabel: stc + " W/m&sup2; &middot; Standard Test Conditions"
      });
      buildMetric(detailContainer, {
        title: "Temperature (&deg;C)", color: "var(--dusk)", values: temperature, zeroBased: false,
        showAxis: true, timestamps: timestamps
      });
    }
    var outlookLabel = (numDays === 1 ? "24-hour" : numDays + "-day") + (hasWeather ? " outlook" : " trend (no weather)");
    document.getElementById("detail-hint").textContent = outlookLabel;

    // hourly table. hasSimulated and hasWeather are independent (ARIMA
    // can have a simulated comparison without ever having weather of
    // its own, see backend/app.py), so the header/rows are built from
    // both rather than one flag implying the other.
    var headerCells = "<th>When</th><th>Power</th>";
    if (hasSimulated) headerCells += "<th>Simulated</th>";
    if (hasWeather) headerCells += "<th>Irradiance</th><th>Temp</th>";
    document.getElementById("hourly-thead").innerHTML = "<tr>" + headerCells + "</tr>";

    var tbody = document.getElementById("hourly-body");
    var rows = power.map(function (v, i) {
      var t = timestamps[i];
      var cls = i === peakIdx ? ' class="peak-row"' : "";
      var when = DOW[t.getDay()] + " " + t.getDate() + ", " + String(t.getHours()).padStart(2, "0") + ":00";
      var cells = "<td>" + when + "</td><td class=\"tnum\">" + Math.round(v).toLocaleString() + " W</td>";
      if (hasSimulated) {
        cells += "<td class=\"tnum\">" + Math.round(simulatedValues[i]).toLocaleString() + " W</td>";
      }
      if (hasWeather) {
        cells += "<td class=\"tnum\">" + irradiance[i].toFixed(1) + " W/m&sup2;</td><td class=\"tnum\">" + temperature[i].toFixed(1) + "&deg;C</td>";
      }
      return "<tr" + cls + ">" + cells + "</tr>";
    }).join("");
    tbody.innerHTML = rows;

    // "your system" rated capacity, spec grid, peak stat + capacity bar
    document.getElementById("rated-value").textContent = ratedTotal.toLocaleString() + " W";
    document.getElementById("rated-sub").textContent = data.specs.pmp.toFixed(1) + " W × " + qty + " panels";
    document.getElementById("spec-voc").textContent = data.specs.voc.toFixed(1) + " V";
    document.getElementById("spec-isc").textContent = data.specs.isc.toFixed(2) + " A";
    document.getElementById("spec-vmp").textContent = data.specs.vmp.toFixed(1) + " V";
    document.getElementById("spec-imp").textContent = data.specs.imp.toFixed(2) + " A";
    document.getElementById("spec-series").textContent = Math.round(data.specs.n_series);
    document.getElementById("spec-parallel").textContent = Math.round(data.specs.n_parallel);

    var pct = Math.round(peakVal / ratedTotal * 100);
    document.getElementById("peak-value").textContent = Math.round(peakVal).toLocaleString() + " W";
    document.getElementById("peak-sub").textContent = pct + "% of rated, " +
      (numDays === 1
        ? "at " + String(peakTime.getHours()).padStart(2, "0") + ":00"
        : "on " + DOW[peakTime.getDay()] + " " + MONTH[peakTime.getMonth()] + " " + peakTime.getDate());
    document.getElementById("capacity-bar-fill").style.width = Math.min(100, pct) + "%";

    // applied-to-your-forecast section in the performance modal: real
    // accuracy for this exact forecast (data.live_metrics, computed
    // server-side against the actual simulation on this same weather),
    // not a projection from old training-time metrics applied to
    // today's peak.
    var appliedRow = document.getElementById("applied-live-row");
    if (data.live_metrics && hasSimulated) {
      var modelNames = { random_forest: "Random Forest", lstm: "LSTM", arima: "ARIMA" };
      var modelColors = { random_forest: "var(--sun-strong)", lstm: "var(--sky)", arima: "var(--dusk)" };
      var modelName = modelNames[data.model] || data.model;
      var simPeak = Math.max.apply(null, simulatedValues);

      document.getElementById("applied-intro-text").innerHTML =
        modelName + " predicted a peak of <strong>" + Math.round(peakVal).toLocaleString() + " W</strong>. " +
        "The actual Simscape Electrical simulation on this same weather peaked at <strong>" +
        Math.round(simPeak).toLocaleString() + " W</strong>.";
      document.getElementById("applied-engine-swatch").style.background = modelColors[data.model] || modelColors.random_forest;
      document.getElementById("applied-engine-name").textContent = modelName;
      document.getElementById("applied-live-error").textContent =
        "±" + data.live_metrics.MAE.toFixed(2) + " W";
      document.getElementById("applied-live-detail").textContent =
        "typical error on this forecast (RMSE " + data.live_metrics.RMSE.toFixed(2) + " W)";
      appliedRow.hidden = false;
    } else {
      document.getElementById("applied-intro-text").textContent =
        "The live simulation comparison wasn't available for this forecast.";
      appliedRow.hidden = true;
    }
  }
})();
