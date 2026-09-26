(function () {
  /* Debug: hands the HTML of the page the user opened to the local app; nothing else. */
  var base = '__BASE__';
  var copy = document.documentElement.cloneNode(true);
  copy.querySelectorAll('script,style,svg,img,iframe,link,meta,noscript,video,picture,source').forEach(function (e) { e.remove(); });
  var data = { source_url: location.origin + location.pathname, html: copy.outerHTML };
  /* Fallback when LinkedIn cuts the link between tabs: the data is also on the clipboard. */
  try { navigator.clipboard.writeText(JSON.stringify(data)); } catch (e) {}
  var w = window.open(base + '/capture/debug', '_blank');
  window.addEventListener('message', function onReady(ev) {
    if (ev.origin !== base || ev.data !== 'job-radar-ready') return;
    window.removeEventListener('message', onReady);
    w.postMessage(data, base);
  });
})();
