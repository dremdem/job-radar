(function () {
  /* Reads the LinkedIn or hh.ru page the user opened; no navigation, clicks or requests. */
  var base = '__BASE__';
  var hh = location.pathname.match(/\/vacancy\/(\d+)/);
  if (/(^|\.)hh\.(ru|kz|by|uz)$/.test(location.hostname) && hh) {
    /* hh.ru vacancy page (#136): fields carry data-qa attributes. */
    var g = function (sel) { var e = document.querySelector(sel); return e ? (e.innerText || e.textContent || '').trim() : ''; };
    var hhData = { source: 'hh', source_url: location.origin + location.pathname, cards: [], detail: {
      job_id: hh[1],
      title: g('[data-qa="vacancy-title"]'),
      company: g('[data-qa="vacancy-company-name"]'),
      salary: g('[data-qa^="vacancy-salary"]'),
      work_format: g('[data-qa="work-formats-text"]'),
      address: g('[data-qa="vacancy-view-raw-address"]') || g('[data-qa="vacancy-view-location"]'),
      description: g('[data-qa="vacancy-description"]').slice(0, 20000)
    } };
    window.open(base + '/capture#' + encodeURIComponent(JSON.stringify(hhData)), '_blank');
    return;
  }
  var seen = {}, cards = [];
  function add(id, el) {
    if (!id || seen[id]) return;
    seen[id] = 1;
    var card = el.closest('li') || el.closest('[data-job-id]') || el;
    cards.push({ job_id: id, text: (card.innerText || '').slice(0, 3000) });
  }
  /* Semantic search (/jobs/search-results/): cards carry componentkey="job-card-component-ref-<id>". */
  document.querySelectorAll('[componentkey^="job-card-component-ref-"]').forEach(function (el) {
    var id = el.getAttribute('componentkey').split('-').pop();
    if (!id || seen[id]) return;
    seen[id] = 1;
    cards.push({ job_id: id, text: (el.innerText || '').slice(0, 3000) });
  });
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
    /* textContent also covers text folded under "… more". */
    var text = function (sel) {
      /* Selectors in priority order; among one selector's matches the longest text wins
         (the job description is longer than the company blurb next to it). */
      var list = sel.split('|');
      for (var i = 0; i < list.length; i++) {
        var best = '';
        document.querySelectorAll(list[i]).forEach(function (e) {
          var raw = e.textContent.trim(), shown = (e.innerText || '').trim();
          /* innerText keeps line breaks; fall back to textContent if text is folded away. */
          var t = shown.length >= raw.length * 0.8 ? shown : raw;
          if (t.length > best.length) best = t;
        });
        if (best) return best;
      }
      return '';
    };
    detail = {
      job_id: cur[1],
      page_title: document.title,
      title: q('.job-details-jobs-unified-top-card__job-title, .jobs-unified-top-card__job-title'),
      company: q('.job-details-jobs-unified-top-card__company-name, .jobs-unified-top-card__company-name'),
      description: text('[componentkey^="JobDetails_AboutTheJob"]|[data-testid="expandable-text-box"]|#job-details|.jobs-description__content|.jobs-description-content__text|[class*="jobs-description"]').slice(0, 20000)
    };
  }
  var data = { source_url: location.origin + location.pathname, cards: cards, detail: detail };
  window.open(base + '/capture#' + encodeURIComponent(JSON.stringify(data)), '_blank');
})();
