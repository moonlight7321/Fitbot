const tg = window.Telegram ? window.Telegram.WebApp : null;
if (tg) { tg.ready(); tg.expand(); }

window.addEventListener("error", event => {
  try { toast("Ошибка: " + (event.message || "неизвестная")); } catch (e) { /* ignore */ }
});

const state = { overview: null, exercises: null, tab: "plan", period: 30 };

function headers() {
  return {
    "X-Init-Data": tg ? tg.initData : "",
    "Content-Type": "application/json",
  };
}

async function api(path, options = {}) {
  const response = await fetch(path, { headers: headers(), ...options });
  if (!response.ok) {
    let message = "HTTP " + response.status;
    try {
      const data = await response.json();
      if (data && (data.message || data.error)) message = data.message || data.error;
    } catch (e) { /* тело не JSON */ }
    throw new Error(message);
  }
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
  state.overview = await api("/api/overview?period=" + state.period);
  setStatus(state.overview.subscription);
  render();
}

async function loadExercises() {
  if (!state.exercises) state.exercises = await api("/api/exercises");
  render();
}

function optionsHtml(map, current) {
  return Object.keys(map || {}).map(key =>
    '<option value="' + esc(key) + '"' + (key === current ? ' selected' : '') + '>'
    + esc(map[key]) + '</option>').join('');
}

function numberOptions(list, current) {
  return (list || []).map(value =>
    '<option value="' + value + '"' + (value === current ? ' selected' : '') + '>'
    + value + '</option>').join('');
}

const DRAFT_KEY = "fitbot_profile_draft";

function loadDraft() {
  try { return JSON.parse(localStorage.getItem(DRAFT_KEY) || "{}") || {}; }
  catch (e) { return {}; }
}

function saveDraft() {
  const form = document.getElementById("profile-form");
  if (!form) return;
  const value = id => {
    const el = document.getElementById(id);
    return el ? el.value : undefined;
  };
  const limits = Array.from(form.querySelectorAll("input[type=checkbox]:checked"))
    .map(input => input.value);
  try {
    localStorage.setItem(DRAFT_KEY, JSON.stringify({
      goal: value("pf-goal"), experience: value("pf-exp"), equipment: value("pf-eq"),
      days: value("pf-days"), minutes: value("pf-min"), activity: value("pf-act"),
      sex: value("pf-sex"), age: value("pf-age"),
      height: value("pf-height"), weight: value("pf-weight"),
      limitations: limits,
    }));
  } catch (e) { /* приватный режим — просто не сохраняем */ }
}

function profileFormCard(data, opts) {
  const options = opts || {};
  const draft = data.profile_raw ? {} : loadDraft();
  const raw = Object.assign({}, draft, data.profile_raw || {});
  const opt = data.options || {};
  const isOpen = Boolean(options.forceOpen) || !data.profile_raw;
  const summary = options.hideToggle ? '' : '<summary class="ghost">⚙️ Анкета и расчёт</summary>';
  const saveLabel = 'Сохранить и рассчитать';
  const limits = (raw.limitations || []);
  const limitBoxes = Object.keys(opt.limitations || {}).map(key =>
    '<label class="chk"><input type="checkbox" value="' + esc(key) + '"'
    + (limits.indexOf(key) >= 0 ? ' checked' : '') + '> ' + esc(opt.limitations[key]) + '</label>').join('');

  return ''
    + '<div class="card"><details id="profile-form"' + (isOpen ? ' open' : '') + '>'
    + summary
    + '<div class="muted" style="margin-top:8px">Заполните один раз — план и калории посчитаются сразу.</div>'
    + '<label class="field">Цель<select id="pf-goal" class="select">' + optionsHtml(opt.goals, raw.goal || 'tone') + '</select></label>'
    + '<label class="field">Опыт<select id="pf-exp" class="select">' + optionsHtml(opt.experience, raw.experience || 'novice') + '</select></label>'
    + '<label class="field">Инвентарь<select id="pf-eq" class="select">' + optionsHtml(opt.equipment, raw.equipment || 'gym') + '</select></label>'
    + '<label class="field">Дней в неделю<select id="pf-days" class="select">' + numberOptions(opt.days, raw.days || 3) + '</select></label>'
    + '<label class="field">Минут на тренировку<select id="pf-min" class="select">' + numberOptions(opt.minutes, raw.minutes || 45) + '</select></label>'
    + '<label class="field">Активность вне зала<select id="pf-act" class="select">' + optionsHtml(opt.activity, raw.activity || 'light') + '</select></label>'
    + '<div class="input-row">'
    + '<label class="field" style="flex:1">Пол<select id="pf-sex" class="select">'
    + '<option value="m"' + (raw.sex === 'm' ? ' selected' : '') + '>Мужской</option>'
    + '<option value="f"' + (raw.sex === 'f' ? ' selected' : '') + '>Женский</option></select></label>'
    + '<label class="field" style="flex:1">Возраст<input id="pf-age" class="select" type="number" min="14" max="90" value="' + (raw.age || 30) + '"></label>'
    + '</div>'
    + '<div class="input-row">'
    + '<label class="field" style="flex:1">Рост, см<input id="pf-height" class="select" type="number" min="100" max="250" value="' + (raw.height || 175) + '"></label>'
    + '<label class="field" style="flex:1">Вес, кг<input id="pf-weight" class="select" type="number" min="30" max="250" value="' + (raw.weight || 70) + '"></label>'
    + '</div>'
    + '<div class="muted" style="margin-top:8px">Ограничения:</div><div class="chips">' + limitBoxes + '</div>'
    + '<label class="chk" style="margin-top:8px"><input type="checkbox" id="pf-reminder"'
    + (raw.weight_reminder ? ' checked' : '') + '> Напоминать о взвешивании раз в две недели</label>'
    + '<button class="primary" id="profile-save" style="margin-top:10px">' + saveLabel + '</button>'
    + '</details></div>';
}

function paywallCard(title, data) {
  const sub = (data && data.subscription) || {};
  const trial = (sub.trial_used || sub.kind !== 'none')
    ? ''
    : '<button class="primary" id="trial-btn" style="margin-top:8px">Попробовать 7 дней бесплатно</button>';
  const paid = sub.kind === 'expired'
    ? '<p class="muted">Подписка закончилась. Продлить можно в боте: /sub</p>'
    : '<p class="muted">Оформить подписку — в боте: /sub</p>';

  return '<div class="card"><h2>' + title + '</h2>'
    + '<p class="muted">Доступно по подписке. Пробный период — 7 дней бесплатно, '
    + 'дальше 349 ₽ за 30 дней.</p>'
    + '<p class="muted">Без подписки остаются калькулятор калорий и 17 базовых упражнений.</p>'
    + trial + paid + '</div>';
}

function bindDraft() {
  const form = document.getElementById("profile-form");
  if (!form || form.dataset.draftBound) return;
  form.dataset.draftBound = "1";
  form.addEventListener("input", saveDraft);
  form.addEventListener("change", saveDraft);
}

async function saveProfile() {
  const value = id => document.getElementById(id).value;
  const limits = Array.from(
    document.querySelectorAll("#profile-form input[type=checkbox]:checked")
  ).map(input => input.value);
  try {
    await api("/api/profile", {
      method: "POST",
      body: JSON.stringify({
        goal: value("pf-goal"), experience: value("pf-exp"), equipment: value("pf-eq"),
        days: Number(value("pf-days")), minutes: Number(value("pf-min")),
        activity: value("pf-act"), sex: value("pf-sex"), age: Number(value("pf-age")),
        height: Number(value("pf-height")), weight: Number(value("pf-weight")),
        limitations: limits,
        weight_reminder: Boolean(document.getElementById("pf-reminder") &&
                                  document.getElementById("pf-reminder").checked),
      }),
    });
    try { localStorage.removeItem(DRAFT_KEY); } catch (e) { /* ignore */ }
    toast("Анкета сохранена");
    await loadOverview();
  } catch (e) { toast(e.message || "Проверьте цифры: рост 100–250, вес 30–250"); }
}

function renderPlan(data) {
  const form = profileFormCard(data);
  if (!data.access) return form + paywallCard("План", data);
  if (!data.profile) {
    return form + '<div class="card"><h2>План</h2><p class="muted">'
      + 'Заполните анкету выше — план построится сразу.</p></div>';
  }
  const p = data.profile;
  const nextIndex = (data.next_day === undefined || data.next_day === null) ? 0 : data.next_day;
  const currentDay = data.plan && data.plan.days.length ? data.plan.days[nextIndex] : null;
  const days = currentDay
    ? '<div class="day"><h3>День ' + (nextIndex + 1) + ' из ' + data.plan.days.length
      + ' · ' + esc(currentDay.title) + '</h3>'
      + '<ol class="list">' + currentDay.slots.map(s =>
          '<li>' + esc(s.name) + ' — ' + setsPhrase(s.sets, esc(s.reps))
          + (s.weight ? ' · <b>' + esc(s.weight) + '</b>' : '') + '</li>').join('') + '</ol></div>'
    : '<p class="muted">Упражнений не нашлось под текущие условия.</p>';

  const deload = data.week && data.week.deload
    ? '<div class="card deload"><b>🟢 Разгрузочная неделя</b>'
      + '<div class="muted">Подходов меньше, вес тот же — так уходит накопленная усталость.'
      + (data.week.reasons && data.week.reasons.length ? ' Причина: ' + esc(data.week.reasons.join(', ')) : '')
      + '</div></div>'
    : '';

  const fullWeek = data.plan.days.map((day, i) =>
    '<div class="day" style="margin-top:8px"><h3>День ' + (i + 1) + ' · ' + esc(day.title) + '</h3>'
    + '<ol class="list">' + day.slots.map(s =>
        '<li>' + esc(s.name) + ' — ' + setsPhrase(s.sets, esc(s.reps))
        + (s.weight ? ' · <b>' + esc(s.weight) + '</b>' : '') + '</li>').join('') + '</ol></div>'
  ).join('');

  return form + deload + ''
    + '<div class="card"><h2>Ваш план</h2>'
    + '<div class="muted">' + esc(p.goal) + ' · ' + esc(p.experience) + ' · ' + esc(p.equipment) + '</div>'
    + '<div class="muted">' + p.days + ' тренировки в неделю по ' + p.minutes + ' минут</div>'
    + '<div class="muted">Ограничения: ' + esc(p.limitations) + '</div>'
    + '<div class="days" style="margin-top:10px">' + days + '</div>'
    + '<div class="input-row" style="margin-top:10px">'
    + '<button class="ghost" id="skip-day">⏭ Пропустить день</button>'
    + '<button class="ghost" id="week-toggle">📅 Вся неделя</button>'
    + '</div>'
    + '<div id="week-full" class="hidden">' + fullWeek + '</div>'
    + '</div>'
    + '<div class="card"><button class="ghost" id="weight-guide-btn">❓ Как подобрать вес</button>'
    + '<div id="weight-guide" class="hidden">'
    + '<p class="muted">Штанга — около 20 % веса тела, гантели — около 10 % на руку '
    + '(в записи указывайте вес одной гантели), тренажёры — самый лёгкий вес, '
    + 'где чувствуется сопротивление.</p>'
    + '<p class="muted">Для вашего веса ' + (data.profile ? data.profile.weight : '—')
    + ' кг это примерно ' + (data.profile ? Math.round(data.profile.weight * 0.2) : '—')
    + ' кг на штанге и ' + (data.profile ? Math.round(data.profile.weight * 0.1) : '—')
    + ' кг на руку.</p>'
    + '<p class="muted">Последние 2 повторения должны даваться тяжело, но без потери техники. '
    + 'Если тяжело или легко — поменяйте вес на следующей тренировке, бот запомнит и будет '
    + 'считать прогрессию от него.</p>'
    + '<p class="muted">Это ориентир, а не назначение.</p>'
    + '</div></div>'
    + renderEditor(data)
    + renderProgress(data)
    + '<div class="card"><p class="muted">' + esc(data.plan.note) + '</p></div>';
}

function repsFromRange(reps) {
  const text = String(reps || "10").replace(/[^0-9–-]/g, "");
  const parts = text.split(/[–-]/);
  return Number(parts[parts.length - 1]) || 10;
}

function renderEditor(data) {
  if (!data.plan || !data.plan.days.length) return '';
  const index = (data.next_day === undefined || data.next_day === null) ? 0 : data.next_day;
  const day = data.plan.days[index];
  const prefill = data.editor_prefill || {};
  const items = day.slots.map(slot => {
    const last = prefill[slot.slug] || {};
    return {
      slug: slot.slug,
      original: slot.slug,
      name: slot.name,
      sets: last.sets || slot.sets,
      reps: last.reps || repsFromRange(slot.reps),
      weight: last.weight !== undefined ? last.weight : (slot.weight_value || 0),
    };
  });
  state.editor = { dayIndex: index, items: items };

  const rows = items.map((item, i) =>
    '<div class="ed-row">'
    + '<button class="ex" data-ed-name="' + i + '">' + esc(item.name) + '</button>'
    + '<select class="select ed-num" data-ed-sets="' + i + '">'
    + [1, 2, 3, 4, 5, 6].map(n => '<option value="' + n + '"' + (n === item.sets ? ' selected' : '') + '>' + n + ' подх.</option>').join('')
    + '</select>'
    + '<select class="select ed-num" data-ed-reps="' + i + '">'
    + [6, 8, 10, 12, 15, 20].map(n => '<option value="' + n + '"' + (n === item.reps ? ' selected' : '') + '>' + n + ' раз</option>').join('')
    + '</select>'
    + '<input class="select ed-num ed-weight" type="number" min="0" max="300" step="0.5" data-ed-weight="' + i + '" value="' + item.weight + '" title="вес, кг">'
    + '<span class="muted">кг</span>'
    + '</div>').join('');

  return ''
    + '<div class="card"><h2>Редактор тренировки</h2>'
    + '<p class="muted">Заполните, как прошла тренировка: подходы, повторения и вес — '
    + 'это нужно для учёта прогресса. Нажмите на название, чтобы заменить упражнение.</p>'
    + '<div id="ed-rows">' + rows + '</div>'
    + '<div id="ed-alt" class="hidden"></div>'
    + '<div class="input-row" style="margin-top:8px">'
    + '<button class="primary" id="ed-save">Записать тренировку</button>'
    + '</div>'
    + '<div class="input-row" style="margin-top:6px">'
    + '<button class="ghost" id="ed-partial">🟡 Частично</button>'
    + '<button class="ghost" id="ed-skip">😕 Не смог</button>'
    + '</div>'
    + '<div id="ed-part" class="hidden">'
    + '<div class="muted" style="margin-top:10px">Отметьте упражнения, которые не получились, '
    + 'и укажите причину — это пойдёт в учёт слабых групп.</div>'
    + '<div id="ed-part-list" class="chips" style="margin-top:6px"></div>'
    + '<label class="field">Причина<select id="ed-part-reason" class="select">'
    + '<option value="reps">не хватило повторений</option>'
    + '<option value="weight">не осилил вес</option>'
    + '<option value="pain">дискомфорт или боль</option>'
    + '</select></label>'
    + '<button class="primary" id="ed-part-send" style="margin-top:8px">Отметить частично</button>'
    + '</div></div>';
}

function renderJournal(data) {
  if (!data.plan || !data.plan.days.length) return '';
  const nextIndex = (data.next_day === undefined || data.next_day === null) ? 0 : data.next_day;
  const options = data.plan.days.map((day, i) =>
    '<option value="' + i + '"' + (i === nextIndex ? ' selected' : '') + '>День ' + (i + 1)
    + ' · ' + esc(day.title) + '</option>').join('');
  const hasWeights = data.plan.days.some(day => day.slots.some(s => s.weight));
  const weightsHint = hasWeights
    ? ' Вписывать упражнения нужно только когда что-то не получилось.'
    : ' После первой тренировки укажите рабочие веса — без цифр бот не сможет '
      + 'подсказывать прогрессию. Для гантелей — вес одной гантели.';

  return ''
    + '<div class="card"><h2>Записать тренировку</h2>'
    + '<p class="muted">Если сделали всё по плану — просто нажмите «Всё выполнил».'
    + weightsHint + '</p>'
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
    + '<div id="feel-box" class="hidden">'
    + '<div class="muted" style="margin-top:10px">Как прошло? Это подскажет, повышать ли вес:</div>'
    + '<div class="input-row">'
    + '<button class="ghost" data-feel="easy">😌 Легко</button>'
    + '<button class="ghost" data-feel="normal">🙂 Нормально</button>'
    + '<button class="ghost" data-feel="hard">😮‍💨 Тяжело</button>'
    + '</div></div>'
    + '<div id="wk-result" class="muted" style="margin-top:8px"></div></div>';
}

function setsPhrase(sets, reps) {
  const mod100 = sets % 100, mod10 = sets % 10;
  let word = "подходов";
  if (mod100 >= 11 && mod100 <= 14) word = "подходов";
  else if (mod10 === 1) word = "подход";
  else if (mod10 >= 2 && mod10 <= 4) word = "подхода";
  return sets + " " + word + " по " + reps + " повторений";
}

function reasonLabel(reason) {
  const names = { reps: "не хватило повторений", weight: "не осилил вес", pain: "дискомфорт" };
  return names[reason] || "без уточнения";
}

function shortDate(iso) {
  return iso.slice(8, 10) + '.' + iso.slice(5, 7);
}

function sparkline(series) {
  const points = (series.points || []).slice();
  const trend = (series.trend || []).slice();
  if (!points.length) return '';
  const w = 340, h = 120, padL = 10, padR = 10, padT = 14, padB = 28;
  const values = points.map(p => p.weight).concat(trend.map(p => p.weight));
  const min = Math.min.apply(null, values);
  const max = Math.max.apply(null, values);
  const span = max - min || 1;
  const step = points.length > 1 ? (w - padL - padR) / (points.length - 1) : 0;
  const y = v => h - padB - ((v - min) / span) * (h - padT - padB);
  const dots = points.map((p, i) => {
    const cx = padL + i * step, cy = y(p.weight);
    return '<circle cx="' + cx.toFixed(1) + '" cy="' + cy.toFixed(1) + '" r="2.6" fill="#4f8cff" />';
  }).join('');
  const coords = points.map((p, i) =>
    (padL + i * step).toFixed(1) + ',' + y(p.weight).toFixed(1)
  ).join(' ');

  let trendLine = '';
  if (trend.length > 1) {
    const trendCoords = trend.map((p, i) =>
      (padL + i * step).toFixed(1) + ',' + y(p.weight).toFixed(1)).join(' ');
    trendLine = '<polyline points="' + trendCoords + '" fill="none" stroke="#f0a94c" '
      + 'stroke-width="2" stroke-dasharray="5 4" />';
  }
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
    + trendLine
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

function caloriesCard(data) {
  const c = data.calories;
  if (!c) {
    return '<div class="card"><h2>Калории</h2><p class="muted">'
      + 'Заполните пол, возраст и активность в анкете — расчёт появится здесь.</p></div>';
  }
  const eaten = (data.diary && data.diary.totals.kcal) || 0;
  const left = c.target - eaten;
  return ''
    + '<div class="card"><h2>Калории</h2>'
    + '<div class="muted">Обмен: ' + c.bmr + ' ккал · с активностью: ' + c.tdee + ' ккал</div>'
    + '<div class="row"><span>Норма</span><b>' + c.target + ' ккал</b></div>'
    + '<div class="row"><span>Съедено</span><b>' + Math.round(eaten) + ' ккал</b></div>'
    + '<div class="row"><span>' + (left >= 0 ? 'Осталось' : 'Перебор') + '</span><b>'
    + Math.abs(Math.round(left)) + ' ккал</b></div>'
    + '<div class="macros">'
    + '<div class="macro"><b>' + c.protein + '</b><span>белки, г</span></div>'
    + '<div class="macro"><b>' + c.fat + '</b><span>жиры, г</span></div>'
    + '<div class="macro"><b>' + c.carbs + '</b><span>углеводы, г</span></div>'
    + '</div></div>';
}

function renderDiary(data) {
  if (!data.access || !data.diary) return paywallCard("Дневник", data);
  const totals = data.diary.totals;
  const target = data.calories ? data.calories.target : null;
  const items = data.diary.items || [];
  const list = items.slice().reverse().map(row =>
    '<li>' + esc(row.name) + ' ' + Math.round(row.grams) + ' г — ' + Math.round(row.kcal) + ' ккал</li>').join('');

  return caloriesCard(data) + ''
    + '<div class="card"><h2>Дневник</h2>'
    + '<div class="row"><span>Съедено</span><b>' + Math.round(totals.kcal) + ' ккал</b></div>'
    + (target ? '<div class="muted">Норма ' + target + ' ккал · осталось ' + Math.round(target - totals.kcal) + '</div>'
              : '<div class="muted">Норма не рассчитана — заполните калории в боте.</div>')
    + '<div class="macros">'
    + '<div class="macro"><b>' + Math.round(totals.protein) + '</b><span>белки</span></div>'
    + '<div class="macro"><b>' + Math.round(totals.fat) + '</b><span>жиры</span></div>'
    + '<div class="macro"><b>' + Math.round(totals.carbs) + '</b><span>углеводы</span></div>'
    + '</div>'
    + '<datalist id="food-names">'
    + (data.foods || []).map(name => '<option value="' + esc(name) + '"></option>').join('')
    + '</datalist>'
    + '<div class="input-row"><input id="meal" type="text" list="food-names" placeholder="курица 150 г, рис 200 г" />'
    + '<button class="primary" id="add-meal">Добавить</button></div>'
    + (list ? '<ul class="list" style="margin-top:10px">' + list + '</ul>' : '<p class="muted">Пока пусто.</p>')
    + '<button class="ghost" id="clear-diary" style="margin-top:10px">Очистить день</button>'
    + '<div class="muted" style="margin-top:14px">Все продукты — нажмите, чтобы подставить название:</div>'
    + '<input id="food-filter" class="select" placeholder="Поиск продукта">'
    + '<div class="chips food-list" id="food-list">'
    + (data.foods || []).map(name =>
        '<button class="ghost food-item" data-food="' + esc(name) + '">' + esc(name) + '</button>').join('')
    + '</div></div>';
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

function barChart(title, points, valueKey, unit, target) {
  if (!points.length) return '';
  const values = points.map(p => Number(p[valueKey]) || 0);
  const max = Math.max.apply(null, values.concat(target ? [target] : [])) || 1;

  const normLeft = target ? Math.min(100, Math.round((target / max) * 100)) : null;

  const rows = points.map(p => {
    const value = Number(p[valueKey]) || 0;
    const width = Math.max(2, Math.round((value / max) * 100));
    const norm = normLeft === null ? '' :
      '<span class="bar-norm" style="left:' + normLeft + '%"></span>';
    const label = p.items && p.items.length
      ? '<details class="bar-wrap"><summary class="bar-row">'
      : '<div class="bar-row">';
    const close = p.items && p.items.length ? '</summary>' : '</div>';
    const details = p.items && p.items.length
      ? '<div class="bar-details">' + p.items.map(item =>
          '<div class="muted">' + esc(item.text) + '</div>').join('') + '</div></details>'
      : '';
    return label
      + '<span class="bar-label">' + esc(p.label) + '</span>'
      + '<span class="bar-track"><span class="bar-fill" style="width:' + width + '%"></span>'
      + norm + '</span>'
      + '<span class="bar-value">' + Math.round(value) + ' ' + unit + '</span>'
      + close + details;
  }).join('');

  return '<div class="chart"><div class="muted">' + esc(title) + '</div>'
    + '<div class="bars">' + rows + '</div>'
    + (target ? '<div class="muted">Норма: ' + Math.round(target) + ' ' + unit + '</div>' : '')
    + '</div>';
}

function renderProgressTab(data) {
  if (!data.access) return paywallCard("Прогресс", data);
  const series = (data.progress && data.progress.series) || [];
  const weekly = (data.progress && data.progress.weekly) || [];
  const nutrition = (data.diary_history || []).map(row => ({
    label: row.day.slice(8, 10) + '.' + row.day.slice(5, 7),
    kcal: row.kcal,
    items: (row.items || []).map(item => ({
      text: item.name + ' ' + Math.round(item.grams) + ' г — ' + Math.round(item.kcal) + ' ккал',
    })),
  }));
  const history = data.history || { weeks: [], experience: 0, total: 0, streak: 0 };
  const details = data.details || [];
  const reminders = data.reminders || { days: [], time: "" };
  const statusNames = { done: "✅", partial: "🟡", skip: "😕", empty: "—" };

  const weekItems = week => (week.days || []).map(day => ({
    text: day.date.slice(8, 10) + '.' + day.date.slice(5, 7)
      + ' · День ' + (day.day_index + 1)
      + (day.title ? ' · ' + day.title : '')
      + ' · ' + day.sets + ' подх. · ' + day.tonnage + ' кг',
  }));

  const tonnage = weekly.map(w => ({
    label: "Нед " + w.week + (w.estimated ? " *" : ""),
    tonnage: w.tonnage,
    items: weekItems(w),
  }));
  const workoutsBars = weekly.map(w => ({
    label: "Нед " + w.week,
    workouts: w.workouts,
    items: weekItems(w),
  }));

  const weeks = (history.weeks || []).map(item =>
    '<div class="muted">Неделя ' + item.week + ': ✅ ' + item.done
    + (item.partial ? ' · 🟡 ' + item.partial : '')
    + (item.skipped ? ' · 😕 ' + item.skipped : '')
    + (item.deload ? ' · разгрузка' : '') + '</div>').join('');

  const days = details.map(item =>
    '<div class="muted">' + esc(item.date.slice(8, 10) + '.' + item.date.slice(5, 7))
    + ' · День ' + (item.day_index + 1) + ' · ' + (statusNames[item.status] || '—')
    + (item.tonnage ? ' · ' + item.tonnage + ' кг' : '')
    + (item.estimated ? ' · оценка: ' + item.estimated + ' подх.' : '')
    + Object.keys(item.reasons || {}).slice(0, 1).map(group =>
        ' · ' + esc(group) + ': ' + Object.keys(item.reasons[group]).length + ' прич.')
      .join('') + '</div>').join('');

  const dayButtons = [1, 2, 3, 4, 5, 6, 7].map(d =>
    '<button class="tab' + (reminders.days.indexOf(d) >= 0 ? ' active' : '') + '" data-rem-day="'
    + d + '">' + ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"][d - 1] + '</button>').join('');

  const periods = [7, 30, 90].map(d =>
    '<button class="tab' + (state.period === d ? ' active' : '') + '" data-period="' + d + '">'
    + d + ' дн.</button>').join('');

  const caloriesChart = nutrition.length
    ? barChart('Калории по дням', nutrition, 'kcal', 'ккал', data.calories ? data.calories.target : null)
    : '<p class="muted">Пока нет записей в дневнике.</p>';

  return ''
    + '<div class="card"><div class="input-row">' + periods + '</div></div>'
    + '<div class="card"><h2>Итог</h2>'
    + '<div class="muted">Тренировок: ' + (data.progress ? data.progress.count : 0)
    + ' · стаж ' + history.experience + ' нед.'
    + (history.streak ? ' · серия ' + history.streak + ' нед.' : '') + '</div>'
    + (data.progress && data.progress.weak_groups && data.progress.weak_groups.length
        ? '<div class="muted">Отстают: ' + data.progress.weak_groups
            .map(w => esc(w.group) + ' (' + w.count + ')').join(', ') + '</div>' : '')
    + '</div>'

    + '<details class="card" open><summary><b>📈 График тренировок</b></summary>'
    + barChart('Тренировок за неделю', workoutsBars, 'workouts', 'шт')
    + '<details style="margin-top:8px"><summary class="muted">📦 Тоннаж за неделю (кг × повторения)</summary>'
    + barChart('Объём за неделю', tonnage, 'tonnage', 'кг')
    + '</details>'
    + (weekly.some(w => w.estimated)
        ? '<div class="muted">* часть подходов восстановлена по плану.</div>'
        : '')
    + '</details>'

    + '<div class="card"><h2>Вес тела</h2>'
    + '<p class="muted">Взвешивайтесь примерно раз в две недели, утром натощак: '
    + 'так виден тренд, а случайные колебания не мешают.</p>'
    + ((data.weight_log || []).length
        ? sparkline({
            name: 'Вес, кг — точки и тренд за 14 дней',
            points: data.weight_log.map(w => ({ date: w.date, weight: w.weight })),
            trend: (data.weight_trend || []).map(w => ({ date: w.date, weight: w.weight })),
          })
        : '<p class="muted">Пока нет записей. Добавьте первый вес ниже.</p>')
    + '<div class="input-row" style="margin-top:8px">'
    + '<input id="weight-input" class="select" type="number" min="30" max="250" step="0.1" placeholder="например 78.5">'
    + '<button class="primary" id="weight-save">Записать вес</button>'
    + '</div>'
    + '<label class="chk" style="margin-top:8px"><input type="checkbox" id="weight-reminder"'
    + (data.weight_reminder ? ' checked' : '') + '> Напоминать о взвешивании раз в две недели</label>'
    + (data.weight_suggest
        ? '<div class="card" style="margin-top:10px;background:#232a33">'
          + '<div class="muted">Вес изменился с ' + data.weight_suggest.profile_weight
          + ' до ' + data.weight_suggest.weight + ' кг. Обновить норму калорий?</div>'
          + '<button class="primary" id="weight-suggest" style="margin-top:6px">Обновить анкету</button>'
          + '</div>'
        : '')
    + '</div>'
    + '<details class="card"' + (nutrition.length ? ' open' : '') + '>'
    + '<summary><b>🍽 Питание по дням</b></summary>'
    + caloriesChart
    + '</details>'

    + '<details class="card"><summary><b>📅 История</b></summary>'
    + ((history.weeks || []).length
        ? (history.weeks || []).map(week => {
            const weekDays = details.filter(item => item.week === week.week);
            const dayLines = weekDays.map(item =>
              '<div class="muted">' + esc(item.date.slice(8, 10) + '.' + item.date.slice(5, 7))
              + ' · День ' + (item.day_index + 1) + ' · ' + (statusNames[item.status] || '—')
              + (item.tonnage ? ' · ' + item.tonnage + ' кг' : '')
              + ((item.estimated_items || []).length
                  ? ' · оценка: ' + item.estimated_items.map(x => esc(x.name) + ' ' + x.count).join(', ')
                  : '')
              + Object.keys(item.reasons || {}).slice(0, 1).map(group =>
                  ' · ' + esc(group) + ': ' + Object.keys(item.reasons[group]).length + ' прич.')
                .join('')
              + (item.kcal ? ' · ~' + item.kcal + ' ккал' : '')
              + '</div>'
              + (item.exercises || []).map(ex =>
                  '<div class="muted" style="padding-left:14px">' + esc(ex.name) + ' — '
                  + ex.sets + '×' + ex.reps + (ex.weight ? ' · ' + ex.weight + ' кг' : '') + '</div>'
                ).join('')).join('');
            return '<details><summary class="muted">Неделя ' + week.week + ': ✅ ' + week.done
              + (week.partial ? ' · 🟡 ' + week.partial : '')
              + (week.skipped ? ' · 😕 ' + week.skipped : '')
              + (week.deload ? ' · разгрузка' : '') + '</summary>'
              + (dayLines || '<div class="muted">Нет записей.</div>')
              + '</details>';
          }).join('')
        : '<p class="muted">Пока пусто.</p>')
    + '</details>'

    + '<details class="card"><summary><b>⏰ Напоминания</b></summary>'
    + '<div class="muted">Дни:</div><div class="input-row" style="flex-wrap:wrap">' + dayButtons + '</div>'
    + '<label class="field">Время<select id="rem-time" class="select">'
    + ["07:00", "09:00", "12:00", "18:00", "19:00", "21:00"].map(t =>
        '<option value="' + t + '"' + (t === reminders.time ? ' selected' : '') + '>' + t + '</option>').join('')
    + '</select></label>'
    + '<button class="primary" id="rem-save" style="margin-top:8px">Сохранить напоминания</button>'
    + '</details>';
}

function render() {
  const data = state.overview;
  const view = document.getElementById("view");
  const tabs = document.querySelector(".tabs");

  if (!data) { view.innerHTML = '<div class="card"><p class="muted">Загрузка…</p></div>'; return; }

  const buildEl = document.getElementById("build");
  if (buildEl && data.build) buildEl.textContent = "v" + data.build;

  // Пока анкеты нет — показываем только её, без остальных вкладок
  if (!data.profile_raw) {
    if (tabs) tabs.classList.add("hidden");
    view.innerHTML = ''
      + '<div class="card"><h2>Добро пожаловать</h2>'
      + '<p class="muted">Заполните анкету — сразу посчитаем план тренировок и калории. '
      + 'Это займёт минуту и делается один раз.</p></div>'
      + profileFormCard(data, { forceOpen: true, hideToggle: true });
    const saveBtn = document.getElementById("profile-save");
    if (saveBtn) saveBtn.onclick = saveProfile;
    bindDraft();
    return;
  }

  if (tabs) tabs.classList.remove("hidden");

  if (state.tab === "plan") view.innerHTML = renderPlan(data);
  else if (state.tab === "exercises") view.innerHTML = renderExercises();
  else if (state.tab === "diary") view.innerHTML = renderDiary(data);
  else if (state.tab === "progress") view.innerHTML = renderProgressTab(data);
  else view.innerHTML = renderDiary(data);

  if (state.tab === "diary" && data.access && data.diary) {
    const foodFilter = document.getElementById("food-filter");
    if (foodFilter) foodFilter.oninput = () => {
      const query = foodFilter.value.trim().toLowerCase().replace(/ё/g, "е");
      document.querySelectorAll("[data-food]").forEach(btn => {
        const name = (btn.dataset.food || "").toLowerCase().replace(/ё/g, "е");
        btn.style.display = !query || name.indexOf(query) >= 0 ? "" : "none";
      });
    };

    document.querySelectorAll("[data-food]").forEach(btn => {
      btn.onclick = () => {
        const input = document.getElementById("meal");
        if (!input) return;
        const name = btn.dataset.food;
        const current = input.value.trim();
        input.value = current ? current + ", " + name + " " : name + " ";
        input.focus();
      };
    });

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

  const trialBtn = document.getElementById("trial-btn");
  if (trialBtn) trialBtn.onclick = async () => {
    try {
      await api("/api/trial", { method: "POST", body: "{}" });
      toast("Пробный период активирован");
      await loadOverview();
    } catch (e) { toast(e.message || "Не удалось активировать"); }
  };

  if (state.tab === "plan") {
    // Кнопку «Анкета и расчёт» обрабатывает делегирование на документе:
    // отдельный onclick здесь приводил к двойному переключению и она «не работала».
    const save = document.getElementById("profile-save");
    if (save) save.onclick = saveProfile;
    bindDraft();
  }

  if (state.tab === "plan" && state.editor) {
    const ed = state.editor;
    document.querySelectorAll("[data-ed-sets]").forEach(sel => {
      sel.onchange = () => { ed.items[Number(sel.dataset.edSets)].sets = Number(sel.value); };
    });
    document.querySelectorAll("[data-ed-reps]").forEach(sel => {
      sel.onchange = () => { ed.items[Number(sel.dataset.edReps)].reps = Number(sel.value); };
    });
    document.querySelectorAll("[data-ed-weight]").forEach(input => {
      input.onchange = () => { ed.items[Number(input.dataset.edWeight)].weight = Number(input.value) || 0; };
    });
    document.querySelectorAll("[data-ed-name]").forEach(btn => {
      btn.onclick = async () => {
        const i = Number(btn.dataset.edName);
        try {
          const res = await api("/api/alternatives/" + encodeURIComponent(ed.items[i].slug));
          const box = document.getElementById("ed-alt");
          box.classList.remove("hidden");
          box.innerHTML = '<div class="muted">Заменить «' + esc(ed.items[i].name) + '»:</div>'
            + res.items.map(item => '<button class="ex" data-alt="' + esc(item.slug)
                + '" data-i="' + i + '">' + esc(item.name) + '</button>').join('');
          box.querySelectorAll("[data-alt]").forEach(altBtn => {
            altBtn.onclick = () => {
              const item = ed.items[Number(altBtn.dataset.i)];
              item.slug = altBtn.dataset.alt;
              item.name = altBtn.textContent;
              item.weight = 0;
              const weightInput = document.querySelector('[data-ed-weight="' + i + '"]');
              if (weightInput) weightInput.value = 0;
              btn.textContent = item.name;
              box.classList.add("hidden");
            };
          });
        } catch (e) { toast("Не удалось получить замены"); }
      };
    });
    const saveEd = document.getElementById("ed-save");
    if (saveEd) saveEd.onclick = async () => {
      try {
        await api("/api/workouts/custom", {
          method: "POST",
          body: JSON.stringify({ day_index: ed.dayIndex, items: ed.items }),
        });
        toast("Тренировка записана");
        await loadOverview();
      } catch (e) { toast(e.message || "Не удалось записать"); }
    };

    const markEd = async (status) => {
      try {
        await api("/api/checks", {
          method: "POST",
          body: JSON.stringify({ day_index: ed.dayIndex, status }),
        });
        toast(status === "partial" ? "Отмечено как частичное" : "День пропущен");
        await loadOverview();
      } catch (e) { toast(e.message || "Не удалось отметить"); }
    };
    const skipEd = document.getElementById("ed-skip");
    if (skipEd) skipEd.onclick = () => markEd("skip");
    const partialEd = document.getElementById("ed-partial");
    if (partialEd) partialEd.onclick = () => {
      const box = document.getElementById("ed-part");
      const list = document.getElementById("ed-part-list");
      list.innerHTML = ed.items.map(item =>
        '<label class="chk"><input type="checkbox" value="' + esc(item.slug) + '"> '
        + esc(item.name) + '</label>').join('');
      box.classList.remove("hidden");

      document.getElementById("ed-part-send").onclick = async () => {
        const slugs = Array.from(list.querySelectorAll("input:checked")).map(input => input.value);
        if (!slugs.length) { toast("Отметьте хотя бы одно упражнение"); return; }
        try {
          await api("/api/checks", {
            method: "POST",
            body: JSON.stringify({
              day_index: ed.dayIndex,
              status: "partial",
              slugs: slugs,
              reason: document.getElementById("ed-part-reason").value,
            }),
          });
          toast("Отмечено как частичное");
          await loadOverview();
        } catch (e) { toast(e.message || "Не удалось отметить"); }
      };
    };
  }

  if (state.tab === "plan" && data.plan && data.plan.days.length) {
    const skipBtn = document.getElementById("skip-day");
    if (skipBtn) skipBtn.onclick = async () => {
      const dayIndex = (data.next_day === undefined || data.next_day === null) ? 0 : data.next_day;
      try {
        await api("/api/skip", { method: "POST", body: JSON.stringify({ day_index: dayIndex }) });
        toast("День пропущен");
        await loadOverview();
      } catch (e) { toast("Не удалось пропустить"); }
    };
    const weekBtn = document.getElementById("week-toggle");
    if (weekBtn) weekBtn.onclick = () => {
      const box = document.getElementById("week-full");
      if (box) box.classList.toggle("hidden");
    };
  }

  if (state.tab === "plan") {
    const guideBtn = document.getElementById("weight-guide-btn");
    if (guideBtn) guideBtn.onclick = () => {
      const box = document.getElementById("weight-guide");
      if (box) box.classList.toggle("hidden");
    };
  }

  if (state.tab === "progress") {
    document.querySelectorAll("[data-period]").forEach(btn => {
      btn.onclick = async () => {
        state.period = Number(btn.dataset.period);
        await loadOverview();
      };
    });
  }

  if (state.tab === "progress" && data.access) {
    const saveWeight = async (apply, override) => {
      const input = document.getElementById("weight-input");
      const value = override !== undefined ? Number(override) : Number(input && input.value);
      try {
        await api("/api/weight", {
          method: "POST",
          body: JSON.stringify({ weight: value, apply }),
        });
        toast(apply ? "Вес записан, норма пересчитана" : "Вес записан");
        await loadOverview();
      } catch (e) { toast(e.message || "Проверьте вес: 30–250 кг"); }
    };
    const weightSave = document.getElementById("weight-save");
    if (weightSave) weightSave.onclick = () => saveWeight(false);
    const weightReminder = document.getElementById("weight-reminder");
    if (weightReminder) weightReminder.onchange = async () => {
      try {
        await api("/api/weight-reminder", {
          method: "POST",
          body: JSON.stringify({ enabled: weightReminder.checked }),
        });
        toast(weightReminder.checked ? "Напоминание включено" : "Напоминание выключено");
      } catch (e) { toast(e.message || "Не удалось сохранить"); }
    };
    const weightSuggest = document.getElementById("weight-suggest");
    if (weightSuggest && data.weight_suggest) {
      weightSuggest.onclick = () => saveWeight(true, data.weight_suggest.weight);
    }

    const selected = new Set((data.reminders && data.reminders.days) || []);
    document.querySelectorAll("[data-rem-day]").forEach(btn => {
      btn.onclick = () => {
        const day = Number(btn.dataset.remDay);
        if (selected.has(day)) selected.delete(day);
        else selected.add(day);
        btn.classList.toggle("active");
      };
    });
    const saveRem = document.getElementById("rem-save");
    if (saveRem) saveRem.onclick = async () => {
      try {
        await api("/api/reminders", {
          method: "POST",
          body: JSON.stringify({
            days: Array.from(selected),
            time: document.getElementById("rem-time").value,
          }),
        });
        toast("Напоминания сохранены");
        await loadOverview();
      } catch (e) { toast(e.message || "Не удалось сохранить"); }
    };
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
