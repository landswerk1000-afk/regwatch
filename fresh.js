/* Отметка «агент выходил на связь».

   Без неё сломавшийся агент выглядит как спокойный день: приложение
   показывает вчерашний отчёт, и отличить «новостей нет» от «сбор не идёт»
   нельзя. Файл status.json агент переписывает каждый прогон — восемь раз
   в сутки, — поэтому он намеренно крошечный.

   Один скрипт на обе страницы: и главная, и указатель должны отвечать
   на вопрос «это точно сегодняшнее?» одинаково. */
(function () {
  'use strict';
  var box = document.getElementById('fresh');
  if (!box) return;

  // Прогоны идут каждые три часа. Восемь — это уже пропущенные два подряд,
  // сутки с лишним — это «что-то сломалось», а не «выходной».
  var WARN = 8, DEAD = 30;

  function hhmm(d) {
    return d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  }

  function when(d) {
    var now = new Date();
    var y = new Date(now.getTime());
    y.setDate(y.getDate() - 1);
    if (d.toDateString() === now.toDateString()) return 'сегодня в ' + hhmm(d);
    if (d.toDateString() === y.toDateString()) return 'вчера в ' + hhmm(d);
    return d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'long' }) +
           ' в ' + hhmm(d);
  }

  fetch('./status.json', { cache: 'no-store' }).then(function (r) {
    if (!r.ok) throw new Error('нет');
    return r.json();
  }).then(function (s) {
    var d = new Date(s && s.checked);
    if (isNaN(d.getTime())) throw new Error('дата не разобралась');

    var hours = (Date.now() - d.getTime()) / 3600000;
    var text = hours > DEAD
      ? 'Агент не выходил на связь с ' + when(d)
      : 'Последняя проверка — ' + when(d);

    box.className = 'fresh' + (hours > WARN ? ' stale' : '');
    box.innerHTML = '<span class="pip"></span><span></span>';
    box.lastChild.textContent = text;
    box.hidden = false;
  }).catch(function () {
    // Нет файла — значит, агент с этой версией ещё не отработал.
    // Молчать здесь правильнее, чем пугать пустой отметкой.
    box.hidden = true;
  });
})();
