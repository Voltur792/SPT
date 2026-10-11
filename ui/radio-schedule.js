(function () {
  'use strict';
  const el = id => document.getElementById(id);
  let rows = [], editing = null, loaded = false, pending = false, polling = false, revision = 0;
  let guildChoices = [], channelChoices = [], channelGuild = '';
  const days = ['Пн','Вт','Ср','Чт','Пт','Сб','Вс'];
  const serviceLabels = {yandex:'Яндекс Музыка',vk:'VK Музыка',spotify:'Spotify'};
  const modes = {yandex:[['wave','Моя волна'],['playlist','Плейлист']],vk:[['library','Мои треки VK'],['playlist','Плейлист']],spotify:[['wave','Мой микс Spotify'],['playlist','Плейлист']]};
  function node(tag, text, cls) { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; }
  function message(text = '', error = false) { el('radioNotice').textContent = text; el('radioNotice').className = 'notice ' + (error ? 'error' : 'ok'); }
  async function request(method, params = {}) {
    const result = await call(method, params);
    const value = typeof result === 'string' ? JSON.parse(result) : result;
    if (!value || typeof value !== 'object') throw new Error('Плагин вернул пустой ответ.');
    if (value.error || value.ok === false) throw new Error(value.error || 'Действие не выполнено.');
    return value;
  }
  async function action(button, operation) {
    button.disabled = true;
    try { await operation(); } catch (error) { message(error.message || 'Не удалось выполнить действие.', true); }
    finally { button.disabled = false; }
  }
  days.forEach((day,index) => {
    const label = node('label',undefined,'day'), input = node('input'); input.type = 'checkbox'; input.value = String(index); input.checked = true;
    label.append(input,document.createTextNode(day)); el('radioDays').append(label);
  });
  function repeatFields() { el('radioDays').hidden = el('radioRepeat').value !== 'weekly'; el('radioDateField').hidden = el('radioRepeat').value !== 'once'; }
  function sourceFields() { el('radioPlaylistFields').hidden = el('radioSource').value !== 'playlist'; }
  function serviceFields() {
    el('radioSource').replaceChildren(...modes[el('radioService').value].map(([value,label]) => new Option(label,value)));
    el('radioPlaylist').replaceChildren(new Option('Сначала загрузите список','')); el('radioPlaylistId').value = ''; sourceFields();
  }
  el('radioRepeat').onchange = repeatFields; el('radioService').onchange = serviceFields; el('radioSource').onchange = sourceFields;
  el('radioPlaylist').onchange = () => { if (el('radioPlaylist').value) el('radioPlaylistId').value = el('radioPlaylist').value; };
  function picks(target, field, choices, choose) {
    el(target).replaceChildren(...choices.map(item => {
      const button = node('button',item.name); button.type = 'button'; button.title = item.id;
      button.setAttribute('aria-pressed',String(el(field).value === item.id));
      button.onclick = () => { el(field).value = item.id; choose?.(item); drawChoices(); };
      return button;
    }));
  }
  function drawChoices() {
    el('radioGuildOptions').replaceChildren(...guildChoices.map(g => new Option(g.name,g.id)));
    el('radioChannelOptions').replaceChildren(...channelChoices.map(c => new Option(c.name,c.id)));
    picks('radioGuildButtons','radioGuild',guildChoices,() => { channelChoices = []; channelGuild = ''; el('radioChannel').value = ''; void loadChannels(); });
    picks('radioChannelButtons','radioChannel',channelChoices,() => { el('radioChannelHint').textContent = 'Канал выбран для этой смены.'; });
  }
  el('radioGuild').oninput = () => { channelChoices = []; channelGuild = ''; el('radioChannel').value = ''; drawChoices(); el('radioChannelHint').textContent = 'Нажмите «Загрузить каналы» для выбранного сервера.'; };
  el('radioChannel').oninput = drawChoices;
  serviceFields();
  function fill(row) {
    editing = row.id;
    for (const [id,key] of [['radioName','name'],['radioStart','start'],['radioEnd','end'],['radioGuild','guild_id'],['radioChannel','channel_id'],['radioEvery','every'],['radioWhen','when']]) el(id).value = row[key];
    el('radioEnabled').checked = row.enabled; el('radioNext').checked = row.announce_next;
    el('radioSkipEmpty').checked = row.skip_empty !== false;
    if (channelGuild !== row.guild_id) { channelChoices = []; channelGuild = ''; }
    drawChoices();
    el('radioRepeat').value = row.date ? 'once' : row.days.length === 7 ? 'daily' : 'weekly'; el('radioDate').value = row.date || '';
    el('radioDays').querySelectorAll('input').forEach(input => input.checked = row.days.includes(Number(input.value))); repeatFields();
    el('radioService').value = row.music.service; serviceFields(); el('radioSource').value = row.music.mode; sourceFields(); el('radioPlaylistId').value = row.music.playlist_id;
    el('radioSave').textContent = 'Сохранить смену'; el('radioCancelEdit').hidden = false; el('radioName').focus();
  }
  function resetEditor() { editing = null; el('radioSave').textContent = 'Добавить смену'; el('radioCancelEdit').hidden = true; el('radioName').value = ''; message(); }
  el('radioCancelEdit').onclick = resetEditor;
  const dateText = value => value ? new Date(value).toLocaleString([], {day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'}) : 'нет следующего запуска';
  function draw(state) {
    rows = state.entries || []; loaded = true; el('radioSave').disabled = pending;
    const active = state.active, row = active && rows.find(r => r.id === active.shift_id);
    el('radioActive').hidden = !active; el('radioStop').hidden = !active;
    el('radioActive').textContent = active ? (row?.name || 'Текущая смена') + ' · до ' + new Date(active.ends_at * 1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}) + (active.stop_requested ? ' · остановка…' : '') : '';
    el('radioStateNotice').textContent = state.storage_error || state.runtime_error || '';
    const stamp = JSON.stringify(rows);
    if (el('radioList').dataset.stamp === stamp) return;
    el('radioList').dataset.stamp = stamp;
    const cards = rows.map(row => {
      const card = node('div',undefined,'radio-shift'); card.setAttribute('role','listitem');
      const frequency = row.date ? row.date : row.days.length === 7 ? 'Каждый день' : row.days.map(d => days[d]).join(', ');
      const mode = modes[row.music.service].find(([key]) => key === row.music.mode)?.[1] || 'Плейлист';
      card.append(node('strong',row.name),node('p',`${row.enabled ? frequency : 'Расписание выключено'} · ${row.start}–${row.end}`),node('p',`${serviceLabels[row.music.service]} · ${mode} · канал ${row.channel_id}`),node('p','Ближайший эфир: ' + dateText(row.next)),node('p',row.result || 'Смена ещё не запускалась'));
      const actions = node('div',undefined,'actions');
      const edit = node('button','Изменить','small'); edit.type = 'button'; edit.onclick = () => fill(row);
      const toggle = node('button',row.enabled ? 'Выключить' : 'Включить','small'); toggle.type = 'button'; toggle.onclick = () => mutate(toggle,'radio_save',{entry:{...row,enabled:!row.enabled}});
      const remove = node('button','Удалить','small danger'); remove.type = 'button'; remove.onclick = () => mutate(remove,'radio_remove',{id:row.id});
      actions.append(edit,toggle,remove); card.append(actions); return card;
    });
    el('radioList').replaceChildren(...(cards.length ? cards : [node('div','Добавьте смену, чтобы Astra выходила в эфир по расписанию.','empty')]));
  }
  async function mutate(button, method, params) {
    if (pending) return;
    pending = true; revision++; el('radioSave').disabled = true;
    await action(button, async () => { const state = await request(method,params); draw(state); if (method === 'radio_remove' && editing === params.id) resetEditor(); message(method === 'radio_stop' ? 'Смена остановлена' : 'Сохранено'); });
    pending = false; revision++; el('radioSave').disabled = !loaded;
  }
  el('radioSave').onclick = async () => {
    if (!loaded || pending) return;
    const entry = { name:el('radioName').value, enabled:el('radioEnabled').checked, start:el('radioStart').value, end:el('radioEnd').value,
      date:el('radioRepeat').value === 'once' ? el('radioDate').value : '', days:el('radioRepeat').value === 'daily' ? [0,1,2,3,4,5,6] : [...el('radioDays').querySelectorAll('input:checked')].map(i => Number(i.value)),
      guild_id:el('radioGuild').value.trim(), channel_id:el('radioChannel').value.trim(), music:{service:el('radioService').value,mode:el('radioSource').value,playlist_id:el('radioPlaylistId').value.trim()},
      every:Number(el('radioEvery').value),when:el('radioWhen').value,announce_next:el('radioNext').checked,skip_empty:el('radioSkipEmpty').checked,...(editing ? {id:editing} : {}) };
    if (el('radioRepeat').value === 'once' && !entry.date) { message('Выберите дату начала.',true); return; }
    await mutate(el('radioSave'),'radio_save',{entry});
  };
  el('radioStop').onclick = () => mutate(el('radioStop'),'radio_stop',{});
  async function link(enable = false) {
    const state = await request('radio_link',{enable});
    el('radioLinkStatus').textContent = state.allowed ? 'Discord подключён · автоэфиры разрешены' : 'Discord подключён · разрешите автоэфиры'; el('radioAllow').hidden = state.allowed;
    if (!editing) { if (!el('radioGuild').value) el('radioGuild').value = state.guild_id || ''; if (!el('radioChannel').value) el('radioChannel').value = state.channel_id || ''; }
    if (el('radioGuild').value === state.guild_id) { channelChoices = state.channels || []; channelGuild = state.guild_id; }
    drawChoices();
  }
  el('radioConnect').onclick = () => action(el('radioConnect'), async () => { el('radioLinkStatus').textContent = 'Проверка связи…'; try { await link(); message(); } catch(error) { el('radioLinkStatus').textContent = 'Связь не установлена'; throw error; } });
  el('radioAllow').onclick = () => action(el('radioAllow'), async () => { await link(true); message('Автоматические эфиры разрешены'); });
  async function loadChannels() { return action(el('radioChannels'), async () => {
    message('Загрузка каналов…'); const guild = el('radioGuild').value.trim(); const result = await request('radio_channels',{guild_id:guild});
    if (el('radioGuild').value.trim() !== guild) return;
    guildChoices = result.guilds || []; channelChoices = result.channels || []; channelGuild = result.guild_id || guild;
    if (!guild) el('radioGuild').value = result.guild_id || ''; if (!el('radioChannel').value) el('radioChannel').value = result.channel_id || '';
    drawChoices();
    el('radioChannelHint').textContent = channelChoices.length ? 'Выберите голосовой канал кнопкой выше.' : guildChoices.length > 1 && !channelGuild ? 'Выберите сервер выше — его каналы загрузятся автоматически.' : 'Нет доступных голосовых каналов. Проверьте права бота или выберите другой сервер.';
    message('Каналов: ' + (result.channels || []).length);
  }); }
  el('radioChannels').onclick = loadChannels;
  el('radioPlaylists').onclick = () => action(el('radioPlaylists'), async () => {
    message('Загрузка плейлистов…'); const service = el('radioService').value; const result = await request('radio_playlists',{service});
    if (el('radioService').value !== service) return;
    el('radioPlaylist').replaceChildren(new Option('Выберите плейлист',''),...(result.playlists || []).map(p => new Option(p.title || p.name || 'Плейлист',p.playlist_id)));
    message('Плейлистов: ' + (result.playlists || []).length);
  });
  async function refresh() {
    if (polling || pending) return; polling = true; const version = revision;
    try { const state = await request('radio_state'); if (!pending && revision === version) draw(state); }
    catch(error) { el('radioStateNotice').textContent = error.message; }
    finally { polling = false; }
  }
  const timer = setInterval(() => { if (el('panel-radio').classList.contains('active')) void refresh(); },2500);
  document.querySelector('[data-tab="radio"]').addEventListener('click', () => void refresh());
  void refresh(); window.addEventListener('beforeunload',()=>clearInterval(timer));
})();
