(function () {
  /* Reads the LinkedIn page the user opened; no navigation, clicks or requests to LinkedIn. */
  var base = '__BASE__';
  var seen = {}, cards = [];
  function add(id, el) {
    if (!id || seen[id]) return;
    seen[id] = 1;
    var card = el.closest('li') || el.closest('[data-job-id]') || el;
    cards.push({ job_id: id, text: (card.innerText || '').slice(0, 3000) });
  }
  document.querySelectorAll('[data-job-id],[data-occludable-job-id]').forEach(function (el) {
    add(el.getAttribute('data-job-id') || el.getAttribute('data-occludable-job-id'), el);
  });
  document.querySelectorAll('a[href*="/jobs/view/"]').forEach(function (a) {
    var m = a.href.match(/\/jobs\/view\/(?:[^\/?]*-)?(\d+)/);
    if (m) add(m[1], a);
  });
  var cur = location.href.match(/currentJobId=(\d+)/) || location.pathname.match(/\/jobs\/view\/(?:[^\/]*-)?(\d+)/);
  var detail = null;
  if (cur) {
    var q = function (sel) { var e = document.querySelector(sel); return e ? e.innerText.trim() : ''; };
    detail = {
      job_id: cur[1],
      title: q('.job-details-jobs-unified-top-card__job-title, .jobs-unified-top-card__job-title, h1'),
      company: q('.job-details-jobs-unified-top-card__company-name, .jobs-unified-top-card__company-name'),
      description: q('#job-details, .jobs-description__content, .jobs-description-content__text, [class*="jobs-description"]').slice(0, 20000)
    };
  }
  var data = { source_url: location.origin + location.pathname, cards: cards, detail: detail };
  window.open(base + '/capture#' + encodeURIComponent(JSON.stringify(data)), '_blank');
})();
