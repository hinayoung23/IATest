/* Native navigation, optional scroll effects. No route interception or form replay. */
(function () {
  'use strict';
  var root = document.documentElement;
  root.classList.add('site-interactive');
  var menu = document.querySelector('.site-menu-toggle');
  var navigation = document.querySelector('.site-nav');
  var preferences = document.querySelector('.site-preferences');
  var reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
  var narrow = window.matchMedia('(max-width: 1180px)');
  function closeMenu() {
    document.body.classList.remove('nav-open');
    if (menu) menu.setAttribute('aria-expanded', 'false');
  }
  if (menu && navigation) {
    menu.addEventListener('click', function () {
      var open = document.body.classList.toggle('nav-open');
      menu.setAttribute('aria-expanded', String(open));
      if (open && preferences) preferences.open = false;
    });
    navigation.addEventListener('click', function (event) {
      if (event.target.closest('a')) closeMenu();
    });
    document.addEventListener('click', function (event) {
      if (!event.target.closest('.site-header')) {
        closeMenu();
        if (preferences) preferences.open = false;
      }
    });
    document.addEventListener('keydown', function (event) {
      if (event.key !== 'Escape') return;
      if (document.body.classList.contains('nav-open')) { closeMenu(); menu.focus(); }
      if (preferences && preferences.open) { preferences.open = false; preferences.querySelector('summary').focus(); }
    });
    narrow.addEventListener('change', closeMenu);
    if (preferences) preferences.addEventListener('toggle', function () { if (preferences.open) closeMenu(); });
  }
  window.addEventListener('pagehide', function () { closeMenu(); if (preferences) preferences.open = false; });

  var reveals = Array.from(document.querySelectorAll('[data-reveal]'));
  var stage = document.querySelector('[data-scroll-stage]');
  var journey = document.querySelector('[data-journey]');
  var chapters = Array.from(document.querySelectorAll('[data-chapter]'));
  var observer = null, frame = 0;
  function revealSetup() {
    if (observer) observer.disconnect();
    root.classList.remove('reveal-enabled');
    if (reduced.matches || !('IntersectionObserver' in window)) return;
    try {
      observer = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;
          entry.target.classList.add('is-visible');
          observer.unobserve(entry.target);
        });
      }, {rootMargin: '0px 0px -25px 0px', threshold: 0.05});
      reveals.forEach(function (element) { observer.observe(element); });
      root.classList.add('reveal-enabled');
    } catch (_) { root.classList.remove('reveal-enabled'); }
  }
  function updateScroll() {
    frame = 0;
    if (document.hidden || reduced.matches) return;
    var rect = stage ? stage.getBoundingClientRect() : null;
    var nearest = -1, distance = Infinity;
    chapters.forEach(function (chapter, index) {
      var bounds = chapter.getBoundingClientRect();
      var delta = Math.abs(bounds.top + bounds.height / 2 - window.innerHeight / 2);
      if (delta < distance) { nearest = index; distance = delta; }
    });
    if (rect && rect.bottom > 0 && rect.top < window.innerHeight) {
      var progress = Math.max(0, Math.min(1, (window.innerHeight - rect.top) / (window.innerHeight + rect.height)));
      stage.style.setProperty('--stage-shift', ((progress - 0.5) * 40).toFixed(2) + 'px');
    }
    if (journey && nearest >= 0) journey.dataset.activeChapter = String(nearest);
  }
  function queueScroll() { if (!frame && (stage || journey) && !reduced.matches) frame = requestAnimationFrame(updateScroll); }
  reduced.addEventListener('change', function () {
    revealSetup();
    if (reduced.matches && stage) stage.style.removeProperty('--stage-shift');
    queueScroll();
  });
  window.addEventListener('scroll', queueScroll, {passive: true});
  window.addEventListener('resize', queueScroll, {passive: true});
  window.addEventListener('pageshow', queueScroll);
  document.addEventListener('visibilitychange', function () {
    if (document.hidden && frame) { cancelAnimationFrame(frame); frame = 0; }
    else queueScroll();
  });
  document.querySelectorAll('[data-use-device]').forEach(function (button) {
    button.addEventListener('click', function () {
      var form = document.querySelector('form[action="/actions/mobile-profile"]');
      if (!form) return;
      form.elements.device_id.value = button.dataset.useDevice;
      form.elements.platform.value = button.dataset.platform;
      form.elements.id.focus();
      form.scrollIntoView({behavior: reduced.matches ? 'instant' : 'smooth', block: 'center'});
    });
  });
  document.querySelectorAll('.mobile-plan-builder').forEach(function (form) {
    var steps = form.querySelector('[data-mobile-plan-steps]');
    var template = form.querySelector('[data-mobile-step-template]');
    var add = form.querySelector('[data-add-mobile-step]');
    function updateSteps() {
      var rows = Array.from(steps.children);
      form.elements.step_count.value = String(rows.length);
      add.disabled = rows.length >= 30;
      rows.forEach(function (row, index) {
        var heading = row.querySelector('.step-heading');
        if (index && heading) heading.textContent = (document.documentElement.lang.startsWith('zh') ? '操作 ' : 'Action ') + (index + 1);
        if (!index) return;
        row.querySelectorAll('[name]').forEach(function (input) {
          input.name = input.name.replace(/^step(?:__INDEX__|\d+)_/, 'step' + index + '_');
        });
      });
      updateModes();
    }
    function updateModes() {
      var selected = form.elements.profile.selectedOptions[0];
      var ios = selected && selected.dataset.platform === 'ios';
      form.querySelectorAll('select[name="mode"], select[name$="_mode"]').forEach(function (select) {
        Array.from(select.options).forEach(function (option) {
          option.hidden = option.disabled = ios ? option.value !== 'keyboard' : option.value === 'keyboard';
        });
        if (!select.selectedOptions.length || select.selectedOptions[0].disabled) select.value = ios ? 'keyboard' : 'semantic';
      });
    }
    add.addEventListener('click', function () {
      if (steps.children.length >= 30) return;
      steps.appendChild(template.content.cloneNode(true));
      updateSteps();
      steps.lastElementChild.querySelector('select').focus();
    });
    steps.addEventListener('click', function (event) {
      var button = event.target.closest('[data-remove-mobile-step]');
      if (button) { button.closest('.plan-step').remove(); updateSteps(); }
    });
    form.elements.profile.addEventListener('change', updateModes);
    updateSteps();
  });
  revealSetup(); queueScroll();
})();
