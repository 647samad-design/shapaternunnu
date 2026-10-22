/* Location maps: pick a delivery / pickup point, or show one.
   Google Maps when the store set a browser key, otherwise Leaflet with
   OpenStreetMap (free, no key). Markup: see partials/location_picker.html. */
(function () {
  'use strict';
  if (window.__lumenMapLoaded) return;
  window.__lumenMapLoaded = true;
  var STATIC = document.currentScript ? document.currentScript.src.replace(/js\/map\.js.*$/, '') : '/static/bees/';
  var leafletLoading = null, googleLoading = null;

  function loadLeaflet() {
    if (window.L) return Promise.resolve();
    if (leafletLoading) return leafletLoading;
    leafletLoading = new Promise(function (resolve, reject) {
      var css = document.createElement('link');
      css.rel = 'stylesheet'; css.href = STATIC + 'vendor/leaflet-1.9.4/leaflet.css';
      document.head.appendChild(css);
      var s = document.createElement('script');
      s.src = STATIC + 'vendor/leaflet-1.9.4/leaflet.js';
      s.onload = function () { resolve(); }; s.onerror = reject;
      document.head.appendChild(s);
    });
    return leafletLoading;
  }

  function loadGoogle(key) {
    if (window.google && window.google.maps) return Promise.resolve();
    if (googleLoading) return googleLoading;
    googleLoading = new Promise(function (resolve, reject) {
      window.__lumenMapsReady = resolve;
      var s = document.createElement('script');
      s.src = 'https://maps.googleapis.com/maps/api/js?key=' + encodeURIComponent(key) + '&callback=__lumenMapsReady&loading=async';
      s.async = true; s.onerror = reject;
      document.head.appendChild(s);
    });
    return googleLoading;
  }

  function round(v) { return Math.round(v * 1e6) / 1e6; }

  function init(box) {
    if (box.dataset.ready) return;
    box.dataset.ready = '1';
    var canvas = box.querySelector('[data-map-canvas]');
    var latIn = box.querySelector('[data-map-lat]'), lngIn = box.querySelector('[data-map-lng]');
    var status = box.querySelector('[data-map-status]');
    var locate = box.querySelector('[data-map-locate]'), clear = box.querySelector('[data-map-clear]');
    var readonly = box.hasAttribute('data-readonly');
    var lat = parseFloat(box.dataset.lat), lng = parseFloat(box.dataset.lng);
    var has = isFinite(lat) && isFinite(lng);
    var api = null;  // {set(lat,lng,zoom), clear()}

    var words = {};
    box.querySelectorAll('[data-map-words] [data-w]').forEach(function (w) { words[w.dataset.w] = w.textContent; });
    function t(key, fallback) { return words[key] || fallback; }
    function say(text) { if (status) status.textContent = text; }
    function store(la, ln) {
      if (latIn) latIn.value = la === null ? '' : round(la);
      if (lngIn) lngIn.value = ln === null ? '' : round(ln);
      if (clear) clear.hidden = la === null;
    }
    function picked(la, ln, how) {
      store(la, ln);
      say(how || t('set', 'Pin set. Drag it to your exact door if needed.'));
    }

    function makeLeaflet() {
      return loadLeaflet().then(function () {
        var L = window.L;
        L.Icon.Default.imagePath = STATIC + 'vendor/leaflet-1.9.4/images/';
        var map = L.map(canvas, {scrollWheelZoom: false, zoomControl: true}).setView(has ? [lat, lng] : [24, 40], has ? 16 : 2);
        L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom: 19, attribution: '&copy; OpenStreetMap'}).addTo(map);
        var marker = null;
        function set(la, ln, zoom) {
          if (!marker) {
            marker = L.marker([la, ln], {draggable: !readonly, keyboard: !readonly}).addTo(map);
            marker.on('dragend', function () { var p = marker.getLatLng(); picked(p.lat, p.lng); });
          } else marker.setLatLng([la, ln]);
          map.setView([la, ln], zoom || Math.max(map.getZoom(), 16));
        }
        if (!readonly) map.on('click', function (e) { set(e.latlng.lat, e.latlng.lng, map.getZoom() < 15 ? 16 : map.getZoom()); picked(e.latlng.lat, e.latlng.lng); });
        if (has) set(lat, lng, 16);
        setTimeout(function () { map.invalidateSize(); }, 200);
        return {set: set, clear: function () { if (marker) { map.removeLayer(marker); marker = null; } }};
      });
    }

    function makeGoogle(key) {
      return loadGoogle(key).then(function () {
        var g = window.google.maps;
        var map = new g.Map(canvas, {center: has ? {lat: lat, lng: lng} : {lat: 24, lng: 40}, zoom: has ? 16 : 2,
          streetViewControl: false, mapTypeControl: false, fullscreenControl: false, gestureHandling: 'cooperative'});
        var marker = null;
        function set(la, ln, zoom) {
          var pos = {lat: la, lng: ln};
          if (!marker) {
            marker = new g.Marker({position: pos, map: map, draggable: !readonly});
            marker.addListener('dragend', function () { var p = marker.getPosition(); picked(p.lat(), p.lng()); });
          } else marker.setPosition(pos);
          map.setCenter(pos);
          map.setZoom(zoom || Math.max(map.getZoom(), 16));
        }
        if (!readonly) map.addListener('click', function (e) { set(e.latLng.lat(), e.latLng.lng()); picked(e.latLng.lat(), e.latLng.lng()); });
        if (has) set(lat, lng, 16);
        return {set: set, clear: function () { if (marker) { marker.setMap(null); marker = null; } }};
      });
    }

    var key = box.dataset.googleKey;
    (key ? makeGoogle(key).catch(function () { return makeLeaflet(); }) : makeLeaflet()).then(function (a) {
      api = a;
      if (!has && box.hasAttribute('data-auto')) findMe(true);
    }).catch(function () { say(t('noload', 'The map could not load. You can still continue without a pin.')); });

    function findMe(quiet) {
      if (!navigator.geolocation) { if (!quiet) say(t('nogeo', 'This browser can\'t share its location. Tap the map instead.')); return; }
      say(t('finding', 'Finding your location…'));
      navigator.geolocation.getCurrentPosition(function (pos) {
        var c = pos.coords;
        if (api) api.set(c.latitude, c.longitude, 17);
        picked(c.latitude, c.longitude, t('found', 'Location found. Drag the pin to your exact door if needed.'));
      }, function () {
        say(quiet ? t('tap', 'Tap the map to drop a pin.') : t('blocked', 'Location access was blocked. Tap the map to drop the pin instead.'));
      }, {enableHighAccuracy: true, timeout: 12000, maximumAge: 60000});
    }
    if (locate) locate.addEventListener('click', function () { findMe(false); });
    if (clear) clear.addEventListener('click', function () { if (api) api.clear(); store(null, null); say(t('removed', 'Pin removed.')); });
    if (clear) clear.hidden = !has;
  }

  function boot() {
    var boxes = document.querySelectorAll('[data-map-picker]');
    if (!boxes.length) return;
    if (!('IntersectionObserver' in window)) { boxes.forEach(init); return; }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) { if (e.isIntersecting) { io.unobserve(e.target); init(e.target); } });
    }, {rootMargin: '200px'});
    boxes.forEach(function (b) { b.hasAttribute('data-auto') ? init(b) : io.observe(b); });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
