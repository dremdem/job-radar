(function () {
  /* Reads the LinkedIn or hh.ru page the user opened; no navigation, clicks or requests.
     The reading itself is capture_read.js, shared with the Chrome fill (#247). */
  var base = '__BASE__';
  var data = (__READ__)();
  window.open(base + '/capture#' + encodeURIComponent(JSON.stringify(data)), '_blank');
})();
