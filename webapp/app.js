const tg = window.Telegram ? window.Telegram.WebApp : null;
if (tg) { tg.ready(); tg.expand(); }

const state = { overview: null, exercises: null, tab: "plan" };

function headers() {
  return {
    "X-Init-Data": tg ? tg.initData : "",
    "Content-Type": "application/json",
  };
}

async function api(path, options = {}) {
  const response = await fetch(path, { headers: headers(), ...options });
  if (!response.ok) throw new Error("HTTP " + response.status);
  return response.json();
}

function toast(text) {
  const el = document.getElementById("toast");
  el.textContent = text;
  el.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 2200);
}

function esc(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function setStatus(sub) {
  const el = document.getElementById("status");
  if (!sub) { el.textContent = "—"; el.className = "badge"; return; }
  if (sub.kind === "paid") { el.textContent = sub.days_left + " дн."; el.className = "badge ok"; }
  else if (sub.kind === "trial") { el.textContent = "триал " + sub.days_left + " дн."; el.className = "badge ok"; }
  else if (sub.kind === "expired") { el.textContent = "истёк"; el.className = "badge warn"; }
  else { el.textContent = "без подписки"; el.className = "badge"; }
}

async function loadOverview() {
  state.overview = await api("/api/overview");
  setStatus(state.overview.subscription);
  render();
}

async function loadExercises() {
  if (!state.exercises) state.exercises = await api("/api/exercises");
  render();
}

function paywallCard(title) {
  return '<div class="card"><h2>' + title + '</h2>'
    + '<p class="muted">Доступно по подписке. Пробный период — 7 дней бесплатно, '
    + 'дальше 349 ₽ за 30 дней.</p>'
    + '<p class="muted">Без подписки остаются калькулятор калорий и 17 базовых упражнений.</p>'
    + '<p class="muted">Оформить можно в боте: /sub</p></div>';
}

function renderPlan(data) {
  if (!data.access) return paywallCard("План");
  if (!data.profile) {
    return '<div class="card"><h2>План</h2><p class="muted">Анкета ещё не заполнена. Откройте бота и нажмите «📋 Мой план».</p></div>';
  }
  const p = data.profile;
  const days = data.plan && data.plan.days.length
    ? data.plan.days.map((day, i) => ''
        + '<div class="day"><h3>День ' + (i + 1) + ' · ' + esc(day.title) + '</h3>'
        + '<ol class="list">' + day.slots.map(s =>
            '<li>' + esc(s.name) + ' — ' + s.sets + '×' + esc(s.reps)
            + (s.weight ? ' · <b>' + esc(s.weight) + '</b>' : '') + '</li>').join('') + '</ol></div>').join('')
    : '<p class="muted">Упражнений не нашлось под текущие условия.</p>';

  const deload = data.week && data.week.deload
    ? '<div class="card deload"><b>🟢 Разгрузочная неделя</b>'
      + '<div class="muted">Подходов меньше, вес тот же — так уходит накопленная усталость.'
      + (data.week.reasons && data.week.reasons.length ? ' Причина: ' + esc(data.week.reasons.join(', ')) : '')
      + '</div></div>'
    : '';

  return deload + ''
    + '<div class="card"><h2>Ваш план</h2>'
    + '<div class="muted">' + esc(p.goal) + ' · ' + esc(p.experience) + ' · ' + esc(p.equipment) + '</div>'
    + '<div class="muted">' + p.days + ' тренировки в неделю по ' + p.minutes + ' минут</div>'
    + '<div class="muted">Ограничения: ' + esc(p.limitations) + '</div>'
    + '<div class="days" style="margin-top:10px">' + days + '</div></div>'
    + renderJournal(data)
    + renderProgress(data)
    + '<div class="card"><p class="muted">' + esc(data.plan.note) + '</p></div>';
}

function renderJournal(data) {
  if (!data.plan || !data.plan.days.length) return '';
  const options = data.plan.days.map((day, i) =>
    '<option value="' + i + '">День ' + (i + 1) + ' · ' + esc(day.title) + '</option>').join('');
  return ''
    + '<div class="card"><h2>Записать тренировку</h2>'
    + '<p class="muted">Если сделали всё по плану — просто нажмите «Всё выполнил». '
    + 'Вписывать упражнения нужно только когда что-то не получилось. '
    + 'Для гантелей указывайте вес одной гантели.</p>'
    + '<select id="wk-day" class="select">' + options + '</select>'
    + '<textarea id="wk-text" class="textarea" rows="3" '
    + 'placeholder="приседания 60x8, 60x8, 60x7&#10;отжимания 0x12, 0x10"></textarea>'
    + '<div class="input-row" style="margin-top:8px">'
    + '<button class="primary" id="wk-complete">✅ Всё выполнил</button>'
    + '<button class="ghost" id="wk-save">✍️ Записать детали</button>'
    + '</div>'
    + '<div class="muted" style="margin-top:10px">Не получилось полностью?</div>'
    + '<div class="input-row">'
    + '<button class="ghost" id="wk-partial">🟡 Частично</button>'
    + '<button class="ghost" id="wk-skip">😕 Не смог</button>'
    + '</div>'
    + '<div id="part-box" class="hidden">'
    + '<div class="muted" style="margin-top:10px">Отметьте упражнения, которые не получились. '
    + 'Причина применится ко всем отмеченным: если у разных упражнений причины разные, '
    + 'отметьте их отдельными заходами — цифры сложатся.</div>'
    + '<div id="part-list" class="ex-grid" style="margin-top:6px"></div>'
    + '<select id="part-reason" class="select">'
    + '<option value="reps">не хватило повторений</option>'
    + '<option value="weight">не осилил вес</option>'
    + '<option value="pain">дискомфорт или боль</option>'
    + '</select>'
    + '<button class="primary" id="part-send" style="margin-top:8px">Отметить частично</button>'
    + '</div>'
    + '<div id="wk-result" class="muted" style="margin-top:8px"></div></div>';
}

function reasonLabel(reason) {
  const names = { reps: "не хватило повторений", weight: "не осилил вес", pain: "дискомфорт" };
  return names[reason] || "без уточнения";
}

function shortDate(iso) {
  return iso.slice(8, 10) + '.' + iso.slice(5, 7);
}

function sparkline(series) {
  const points = series.points || [];
  if (points.length < 2) return '';
  const w = 340, h = 120, padL = 10, padR = 10, padT = 14, padB = 28;
  const values = points.map(p => p.weight);
  const min = Math.min.apply(null, values);
  const max = Math.max.apply(null, values);
  const span = max - min || 1;
  const step = (w - padL - padR) / (points.length - 1);
  const y = v => h - padB - ((v - min) / span) * (h - padT - padB);
  const dots = points.map((p, i) => {
    const cx = padL + i * step, cy = y(p.weight);
    return '<circle cx="' + cx.toFixed(1) + '" cy="' + cy.toFixed(1) + '" r="2.6" fill="#4f8cff" />';
  }).join('');
  const coords = points.map((p, i) =>
    (padL + i * step).toFixed(1) + ',' + y(p.weight).toFixed(1)
  ).join(' ');
  // До шести подписанных дат, равномерно по истории
  const tickCount = Math.min(6, points.length);
  const tickIndexes = [];
  for (let k = 0; k < tickCount; k++) {
    tickIndexes.push(Math.round(k * (points.length - 1) / Math.max(1, tickCount - 1)));
  }
  const uniqueTicks = tickIndexes.filter((v, i) => tickIndexes.indexOf(v) === i);
  const grid = uniqueTicks.map(i =>
    '<line x1="' + (padL + i * step).toFixed(1) + '" y1="' + padT + '" x2="'
    + (padL + i * step).toFixed(1) + '" y2="' + (h - padB) + '" stroke="#232a33" stroke-width="1" />'
  ).join('');
  const labels = uniqueTicks.map((i, order) => {
    const x = padL + i * step;
    const anchor = order === 0 ? 'start' : (order === uniqueTicks.length - 1 ? 'end' : 'middle');
    return '<text x="' + x.toFixed(1) + '" y="' + (h - 8) + '" font-size="10" fill="#8b94a3" text-anchor="'
      + anchor + '">' + shortDate(points[i].date) + '</text>';
  }).join('');

  return ''
    + '<div class="chart">'
    + '<div class="muted">' + esc(series.name) + ' · ' + min + ' → ' + max + ' кг'
    + ' · точек: ' + points.length + '</div>'
    + '<svg viewBox="0 0 ' + w + ' ' + h + '">'
    + grid
    + '<line x1="' + padL + '" y1="' + (h - padB) + '" x2="' + (w - padR) + '" y2="' + (h - padB)
    + '" stroke="#2c343f" stroke-width="1" />'
    + '<polyline points="' + coords + '" fill="none" stroke="#4f8cff" stroke-width="2.5" />'
    + dots
    + labels
    + '</svg></div>';
}

function renderProgress(data) {
  const progress = data.progress;
  if (!progress) return '';
  const items = progress.items.map(item => {
    if (item.first <= 0) {
      return '<li>' + esc(item.name) + ': свой вес, последний раз '
        + esc(item.last_date.slice(8, 10) + '.' + item.last_date.slice(5, 7)) + '</li>';
    }
    if (!item.delta) return '<li>' + esc(item.name) + ': ' + item.last + ' кг</li>';
    const sign = item.delta > 0 ? '+' : '−';
    return '<li>' + esc(item.name) + ': ' + item.first + ' → ' + item.last
      + ' кг (' + sign + Math.abs(item.delta) + ')</li>';
  }).join('');

  return ''
    + '<div class="card"><h2>Прогресс</h2>'
    + '<div class="muted">Тренировок записано: ' + progress.count
    + (progress.experience ? ' · стаж ' + progress.experience + ' нед.' : '')
    + (progress.streak ? ' · серия ' + progress.streak + ' нед.' : '') + '</div>'
    + ((progress.weak_groups || []).length
        ? '<div class="muted">Отстают: ' + progress.weak_groups
            .map(w => esc(w.group) + ' (' + w.count + ')').join(', ') + '</div>'
        : '')
    + Object.keys(progress.breakdown || {}).map(group =>
        '<div class="muted">' + esc(group) + ': ' + Object.entries(progress.breakdown[group])
          .map(([reason, count]) => reasonLabel(reason) + ' ×' + count).join(', ') + '</div>'
      ).join('')
    + (items ? '<ul class="list" style="margin-top:8px">' + items + '</ul>'
             : '<p class="muted">Пока пусто — запишите первую тренировку.</p>')
    + (progress.series || []).map(sparkline).join('')
    + '</div>';
}

function renderExercises() {
  if (!state.exercises) return '<div class="card"><p class="muted">Загрузка…</p></div>';
  return state.exercises.groups.map(group => ''
    + '<div class="card"><h2>' + esc(group.group) + '</h2><div class="ex-grid">'
    + group.items.map(item => ''
        + '<button class="ex ' + (item.locked ? 'locked' : '') + '" data-slug="' + esc(item.slug) + '">'
        + esc(item.name) + (item.locked ? ' 🔒' : '')
        + ' <small>' + esc(item.level) + '</small></button>').join('')
    + '</div></div>').join('');
}

function renderDiary(data) {
  if (!data.access || !data.diary) return paywallCard("Дневник");
  const totals = data.diary.totals;
  const target = data.calories ? data.calories.target : null;
  const items = data.diary.items || [];
  const list = items.slice().reverse().map(row =>
    '<li>' + esc(row.name) + ' ' + Math.round(row.grams) + ' г — ' + Math.round(row.kcal) + ' ккал</li>').join('');

  return ''
    + '<div class="card"><h2>Дневник</h2>'
    + '<div class="row"><span>Съедено</span><b>' + Math.round(totals.kcal) + ' ккал</b></div>'
    + (target ? '<div class="muted">Норма ' + target + ' ккал · осталось ' + Math.round(target - totals.kcal) + '</div>'
              : '<div class="muted">Норма не рассчитана — заполните калории в боте.</div>')
    + '<div class="macros">'
    + '<div class="macro"><b>' + Math.round(totals.protein) + '</b><span>белки</span></div>'
    + '<div class="macro"><b>' + Math.round(totals.fat) + '</b><span>жиры</span></div>'
    + '<div class="macro"><b>' + Math.round(totals.carbs) + '</b><span>углеводы</span></div>'
    + '</div>'
    + '<div class="input-row"><input id="meal" type="text" placeholder="курица 150, рис 200" />'
    + '<button class="primary" id="add-meal">Добавить</button></div>'
    + (list ? '<ul class="list" style="margin-top:10px">' + list + '</ul>' : '<p class="muted">Пока пусто.</p>')
    + '<button class="ghost" id="clear-diary" style="margin-top:10px">Очистить день</button>'
    + '</div>';
}

function renderCalories(data) {
  if (!data.calories) {
    return '<div class="card"><h2>Калории</h2><p class="muted">Нужны пол, возраст и активность — откройте «🧮 Калории» в боте.</p></div>';
  }
  const c = data.calories;
  return ''
    + '<div class="card"><h2>Калории</h2>'
    + '<div class="row"><span>Базовый обмен</span><b>' + c.bmr + '</b></div>'
    + '<div class="row"><span>С активностью</span><b>' + c.tdee + '</b></div>'
    + '<div class="row"><span>Цель</span><b>' + c.target + ' ккал</b></div>'
    + '<div class="macros">'
    + '<div class="macro"><b>' + c.protein + '</b><span>белки, г</span></div>'
    + '<div class="macro"><b>' + c.fat + '</b><span>жиры, г</span></div>'
    + '<div class="macro"><b>' + c.carbs + '</b><span>углеводы, г</span></div>'
    + '</div>'
    + (c.floor_applied ? '<p class="muted">Калории подняты до безопасного минимума.</p>' : '')
    + '<p class="muted">Ориентир, не назначение. Точность ±10–15 %.</p>'
    + '</div>';
}

function render() {
  const data = state.overview;
  const view = document.getElementById("view");

  if (!data) { view.innerHTML = '<div class="card"><p class="muted">Загрузка…</p></div>'; return; }

  if (state.tab === "plan") view.innerHTML = renderPlan(data);
  else if (state.tab === "exercises") view.innerHTML = renderExercises();
  else if (state.tab === "diary") view.innerHTML = renderDiary(data);
  else view.innerHTML = renderCalories(data);

  if (state.tab === "diary" && data.access && data.diary) {
    document.getElementById("add-meal").onclick = async () => {
      const input = document.getElementById("meal");
      const text = input.value.trim();
      if (!text) return;
      try {
        const result = await api("/api/diary/add", { method: "POST", body: JSON.stringify({ text }) });
        if (result.errors.length) toast(result.errors.join("; "));
        else toast("Добавлено: " + result.added);
        await loadOverview();
      } catch (e) { toast("Не получилось добавить"); }
    };
    document.getElementById("clear-diary").onclick = async () => {
      try { await api("/api/diary/clear", { method: "POST", body: "{}" }); await loadOverview(); }
      catch (e) { toast("Ошибка очистки"); }
    };
  }

  if (state.tab === "plan" && data.plan && data.plan.days.length) {
    const complete = document.getElementById("wk-complete");
    if (complete) complete.onclick = async () => {
      const day = document.getElementById("wk-day").value;
      try {
        await api("/api/workouts/complete", {
          method: "POST",
          body: JSON.stringify({ day_index: Number(day) }),
        });
        toast("День записан полностью");
        await loadOverview();
      } catch (e) { toast("Не получилось записать"); }
    };

    const save = document.getElementById("wk-save");
    if (save) save.onclick = async () => {
      const day = document.getElementById("wk-day").value;
      const text = document.getElementById("wk-text").value.trim();
      if (!text) return;
      try {
        const result = await api("/api/workouts", {
          method: "POST",
          body: JSON.stringify({ day_index: Number(day), text }),
        });
        const out = document.getElementById("wk-result");
        if (result.advice && result.advice.length) {
          out.innerHTML = '<b>Следующая тренировка:</b><br>'
            + result.advice.map(a => esc(a.name) + ' → ' + esc(a.text)).join('<br>');
        } else if (result.errors && result.errors.length) {
          out.textContent = result.errors.join("; ");
        } else {
          out.textContent = "Не удалось разобрать записи";
        }
        if (result.added) await loadOverview();
      } catch (e) { toast("Не получилось записать"); }
    };

    const mark = async (status) => {
      const day = document.getElementById("wk-day").value;
      try {
        await api("/api/checks", {
          method: "POST",
          body: JSON.stringify({ day_index: Number(day), status }),
        });
        toast(status === "partial" ? "Отмечено как частичное" : "День пропущен");
        await loadOverview();
      } catch (e) { toast("Не удалось отметить"); }
    };
    const partialButton = document.getElementById("wk-partial");
    if (partialButton) partialButton.onclick = () => {
      const box = document.getElementById("part-box");
      const dayIndex = Number(document.getElementById("wk-day").value);
      const day = data.plan.days[dayIndex];
      const list = document.getElementById("part-list");
      const selected = new Set();
      list.innerHTML = day.slots.map(s =>
        '<button class="ex" data-slug="' + esc(s.slug) + '">' + esc(s.name) + '</button>').join('');
      list.querySelectorAll(".ex").forEach(btn => {
        btn.onclick = () => {
          const slug = btn.dataset.slug;
          if (selected.has(slug)) { selected.delete(slug); btn.classList.remove("selected"); }
          else { selected.add(slug); btn.classList.add("selected"); }
        };
      });
      box.classList.remove("hidden");
      const send = document.getElementById("part-send");
      send.onclick = async () => {
        if (!selected.size) { toast("Отметьте хотя бы одно упражнение"); return; }
        try {
          await api("/api/checks", {
            method: "POST",
            body: JSON.stringify({
              day_index: dayIndex,
              status: "partial",
              slugs: Array.from(selected),
              reason: document.getElementById("part-reason").value,
            }),
          });
          toast("Отмечено как частичное");
          await loadOverview();
        } catch (e) { toast("Не удалось отметить"); }
      };
    };
    const skipButton = document.getElementById("wk-skip");
    if (skipButton) skipButton.onclick = () => mark("skip");
  }

  if (state.tab === "exercises") {
    document.querySelectorAll(".ex").forEach(button => {
      button.onclick = async () => {
        try {
          const ex = await api("/api/exercise/" + button.dataset.slug);
          showExercise(ex);
        } catch (e) { toast("Не удалось открыть упражнение"); }
      };
    });
  }
}

function showExercise(ex) {
  const view = document.getElementById("view");
  view.innerHTML = ''
    + '<div class="card">'
    + '<img class="ex-photo" src="' + ex.images[0] + '" alt="" />'
    + '<h2>' + esc(ex.name) + '</h2>'
    + '<div class="muted">' + esc(ex.level) + ' · ' + esc(ex.equipment) + '</div>'
    + '<div class="muted">Мышцы: ' + esc(ex.muscles.join(", ")) + '</div>'
    + '<h3>Как делать</h3><ol class="list">' + ex.cues.map(c => '<li>' + esc(c) + '</li>').join('') + '</ol>'
    + '<h3>Частые ошибки</h3><ul class="list">' + ex.mistakes.map(m => '<li>✗ ' + esc(m) + '</li>').join('') + '</ul>'
    + '<p class="muted">' + esc(ex.safety) + '</p>'
    + '<button class="ghost" id="back">← Назад</button>'
    + '</div>';
  document.getElementById("back").onclick = render;
}

document.querySelectorAll(".tab").forEach(tab => {
  tab.onclick = () => {
    document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
    tab.classList.add("active");
    state.tab = tab.dataset.tab;
    render();
    if (state.tab === "exercises" && !state.exercises) loadExercises();
  };
});

loadOverview().catch(() => {
  document.getElementById("view").innerHTML =
    '<div class="card"><p class="muted">Не удалось загрузить данные. Откройте приложение через кнопку в боте.</p></div>';
});
