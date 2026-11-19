'use strict';

const POLL_MS = 1000;
const MAX_ROWS = 50;
const STAGES = [['login', 'Login'], ['signin', 'Sign-in'], ['vault', 'Vault'], ['claim', 'Claim']];
const STATUS = {
  idle: ['Idle', 'pill-muted'],
  starting: ['Starting', 'pill-amber'],
  running: ['Running', 'pill-jewel'],
  stopping: ['Stopping', 'pill-amber'],
  finished: ['Finished', 'pill-jewel'],
  stopped: ['Stopped', 'pill-amber'],
  crashed: ['Crashed', 'pill-coral'],
};
const RESULTS = {
  claimed: 'Claimed',
  already: 'Already claimed',
  failed: 'Failed',
  running: 'Running',
  unfinished: 'Unfinished',
};
const COLORS = { claimed: '#3DD6C0', already: '#F2B84B', failed: '#FF9478' };
const NETWORK_WARNINGS = {
  checking: 'Still checking the connection.',
  no_internet: 'No internet.',
  site_unreachable: 'Game site unreachable.',
  unknown: 'Connection status unknown.',
};
const NETWORK_BANNERS = {
  no_internet: 'No internet. Accounts may be failing. The run has not been paused.',
  site_unreachable: 'Game site unreachable. Accounts may be failing. The run has not been paused.',
};
const CONCURRENCY_CHOICES = [1, 2, 3, 5];
const RESTART_NOTICE = 'The dashboard restarted. Your last action was not repeated.';

const token = document.querySelector('meta[name="gow-token"]').content;
const serverId = document.querySelector('meta[name="gow-server"]').content;
const $ = (id) => document.getElementById(id);

// Only choices made on this page live here. Run state always comes from the server
const view = {
  concurrency: 3,
  filter: 'all',
  query: '',
  latest: null,
  offline: false,
  notice: '',
  gate: null,
  sending: false,
  reloading: false,
};

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function number(value) {
  return Number(value).toLocaleString('en-US');
}

function duration(seconds) {
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.round(seconds / 60);
  return minutes >= 60 ? `${Math.floor(minutes / 60)}h ${minutes % 60}m` : `${minutes}m`;
}

function exactDuration(seconds) {
  const minutes = Math.floor(seconds / 60);
  if (minutes >= 60) return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
  return minutes ? `${minutes}m ${seconds % 60}s` : `${seconds}s`;
}

function clock(ts) {
  return new Date(ts * 1000).toLocaleTimeString('en-GB');
}

function setText(id, text) {
  $(id).textContent = text;
}

function renderAlert(state) {
  let text = view.notice;
  if (view.offline) {
    text = 'Dashboard not responding';
  } else if (state && state.control.error) {
    text = state.control.error;
  } else if (state && state.quiet_seconds !== null && state.quiet_seconds >= state.quiet_warning_seconds) {
    text = `No activity for ${duration(state.quiet_seconds)}. The run is still alive.`;
  }

  $('alert').textContent = text;
  $('alert').hidden = !text;
}

function networkState() {
  if (view.offline || !view.latest) return 'unknown';
  return view.latest.network.state;
}

function renderNetwork() {
  const name = networkState();
  const network = view.latest ? view.latest.network : null;
  const ago = network && network.checked_seconds_ago !== null ? `checked ${duration(network.checked_seconds_ago)} ago` : '';
  const texts = {
    unknown: view.latest ? 'Connection status unknown' : 'Checking connection…',
    checking: 'Checking connection…',
    no_internet: `No internet · ${ago}`,
    site_unreachable: `Internet OK, game site unreachable · ${ago}`,
  };

  if (name === 'ok') {
    const ms = network.internet_ms === null ? network.site_ms : network.internet_ms;
    $('network').textContent = `Online · game site OK · ${ms}ms · ${ago}`;
  } else {
    $('network').textContent = texts[name];
  }

  const tones = { ok: 'ok', checking: 'warn', site_unreachable: 'warn', no_internet: 'bad' };
  $('network-dot').className = `net-dot net-dot-${tones[name] || 'muted'}`;

  const banner = view.latest && view.latest.live && !view.offline ? NETWORK_BANNERS[name] : '';
  $('network-banner').textContent = banner || '';
  $('network-banner').hidden = !banner;
}

function renderGate() {
  // Closes without starting anything: a click was made against a warning that no
  // longer applies, or against a run that has since begun
  const busy = view.latest && (view.latest.live || view.latest.control.owned);
  if (view.gate && (networkState() === 'ok' || busy)) view.gate = null;

  $('gate').hidden = !view.gate;
  if (!view.gate) return;

  $('gate-text').textContent = `${NETWORK_WARNINGS[networkState()]} Accounts may fail.`;
  $('gate-go').textContent = view.gate.label;
}

function renderHeader(state) {
  const [label, tone] = STATUS[state.status] || STATUS.idle;
  $('status').textContent = label;
  $('status').className = `pill ${tone}`;

  const primary = $('primary');
  const { phase, owned } = state.control;
  const source = state.run ? state.run.source : null;

  primary.dataset.action = '';
  primary.disabled = true;
  if (phase === 'stopping') {
    primary.textContent = 'Stopping';
  } else if (owned) {
    primary.textContent = 'Stop';
    primary.dataset.action = 'stop';
    primary.disabled = false;
  } else if (state.live) {
    primary.textContent = source === 'terminal' ? 'Running in terminal' : 'Stop unavailable';
  } else {
    primary.textContent = 'Start run';
    primary.dataset.action = 'start';
    primary.disabled = false;
  }

  const count = state.retry_lines.length;
  const busy = owned || state.live;
  $('retry').textContent = count ? `Retry ${count} failed` : 'Nothing to retry';
  $('retry').disabled = !count || busy;

  document.querySelectorAll('[data-concurrency]').forEach((button) => {
    button.setAttribute('aria-pressed', String(Number(button.dataset.concurrency) === view.concurrency));
    button.disabled = busy;
  });
}

function renderKpis(state) {
  const totals = state.totals;
  if (!totals) {
    ['jewels', 'done', 'already', 'failed', 'left', 'rate'].forEach((name) => {
      setText(`kpi-${name}`, '–');
      setText(`kpi-${name}-note`, 'No runs yet');
    });
    return;
  }

  const percent = totals.total ? Math.round((totals.done / totals.total) * 100) : 0;
  setText('kpi-jewels', number(totals.jewels));
  setText('kpi-jewels-note', `${number(totals.characters)} characters checked`);
  setText('kpi-done', `${number(totals.done)} / ${number(totals.total)}`);
  setText('kpi-done-note', totals.unfinished ? `${totals.unfinished} unfinished` : `${percent}% complete`);
  setText('kpi-already', number(totals.already));
  setText('kpi-already-note', 'Cooldown still running');
  setText('kpi-failed', number(totals.failed));
  setText('kpi-failed-note', state.retry_lines.length ? 'Can be retried' : 'Nothing to retry');

  if (totals.seconds_left !== null) {
    setText('kpi-left', duration(totals.seconds_left));
    setText('kpi-left-note', `About ${totals.seconds_per_account}s per account`);
  } else if (state.live) {
    setText('kpi-left', '–');
    setText('kpi-left-note', 'Waiting for the first result');
  } else {
    setText('kpi-left', state.status === 'finished' ? 'Done' : '–');
    setText('kpi-left-note', state.run ? `Took ${duration(state.run.seconds)}` : 'No runs yet');
  }

  const { claims, seconds_per_claim: rate, seconds } = state.run;
  setText('kpi-rate', rate === null ? '–' : `${rate.toFixed(1)}s`);
  setText('kpi-rate-note', rate === null
    ? 'No claims yet'
    : `${number(claims)} ${claims === 1 ? 'claim' : 'claims'} in ${exactDuration(seconds)}`);
}

function renderProgress(state) {
  const totals = state.totals;
  const text = totals
    ? `${number(totals.done)} of ${number(totals.total)} accounts: ${totals.claimed} claimed, `
      + `${totals.already} already claimed, ${totals.failed} failed`
    : 'No run yet';
  setText('progress-text', text);
  $('progress-bar').setAttribute('aria-label', text);

  const whole = totals ? Math.max(totals.total, 1) : 1;
  ['claimed', 'already', 'failed'].forEach((name) => {
    $(`bar-${name}`).style.width = `${totals ? (totals[name] / whole) * 100 : 0}%`;
  });

  const count = state.run ? state.run.concurrency : view.concurrency;
  const cards = [];
  for (let index = 0; index < Math.min(count, 5); index += 1) {
    cards.push(slotCard(state.slots[index]));
  }
  $('slots').replaceChildren(...cards);
}

function slotCard(slot) {
  const card = node('div', 'slot');
  const head = node('div', 'slot-head');
  const position = slot ? STAGES.findIndex(([name]) => name === slot.stage) : -1;

  head.append(
    node('span', 'slot-name', slot ? slot.label : 'Idle'),
    node('span', 'slot-stage', position >= 0 ? STAGES[position][1] : slot ? 'Starting' : ''),
  );

  const steps = node('div', 'slot-steps');
  STAGES.forEach(([, label], index) => {
    const state = index < position ? ' step-done' : index === position ? ' step-now' : '';
    const step = node('div', `step${state}`);
    step.append(node('div', 'step-line'), node('span', '', label));
    steps.append(step);
  });

  card.append(head, steps);
  return card;
}

function renderChart(state) {
  const top = Math.max(...state.days.map((day) => day.jewels), 1);
  const bars = state.days.map((day) => {
    const column = node('div', 'day');
    column.title = `${day.date}: ${number(day.jewels)} jewels`;
    const bar = node('div', day.today ? 'day-bar fill today' : 'day-bar fill');
    bar.style.width = 'auto';
    bar.style.height = `${(day.jewels / top) * 86}%`;
    column.append(bar, node('span', 'day-label', String(day.day)));
    return column;
  });
  $('chart').replaceChildren(...bars);
  $('chart-empty').hidden = state.days.some((day) => day.jewels > 0);
}

function renderResults(state) {
  const totals = state.totals;
  const done = totals ? totals.done : 0;
  const ring = $('ring');

  $('results-empty').hidden = done > 0;
  ['claimed', 'already', 'failed'].forEach((name) => {
    setText(`legend-${name}`, totals ? number(totals[name]) : '–');
  });

  if (!done) {
    ring.style.background = '';
    ring.setAttribute('aria-label', 'No results yet');
    setText('ring-text', '–');
  } else {
    const claimed = (totals.claimed / done) * 100;
    const already = claimed + (totals.already / done) * 100;
    ring.style.background = `conic-gradient(${COLORS.claimed} 0 ${claimed}%, `
      + `${COLORS.already} ${claimed}% ${already}%, ${COLORS.failed} ${already}% 100%)`;
    const rate = Math.round(((totals.claimed + totals.already) / done) * 100);
    ring.setAttribute('aria-label', `${rate}% of accounts done without failing`);
    setText('ring-text', `${rate}%`);
  }

  const worst = Math.max(...state.reasons.map((reason) => reason.count), 1);
  $('reasons-empty').hidden = state.reasons.length > 0;
  $('reasons').replaceChildren(...state.reasons.map((reason) => {
    const row = node('div', 'reason');
    const head = node('div', 'reason-head');
    head.append(node('span', '', reason.label), node('span', '', String(reason.count)));
    const bar = node('div', 'reason-bar');
    const fill = node('div', 'reason-fill');
    fill.style.width = `${(reason.count / worst) * 100}%`;
    bar.append(fill);
    row.append(head, bar);
    return row;
  }));
}

function matchingAccounts(state) {
  const needle = view.query.trim().toLowerCase();
  return state.accounts
    .filter((account) => view.filter === 'all' || account.result === view.filter)
    .filter((account) => !needle
      || account.label.toLowerCase().includes(needle)
      || String(account.line).includes(needle));
}

function renderAccounts(state) {
  const matches = matchingAccounts(state);
  const shown = matches.slice(0, MAX_ROWS);

  $('rows').replaceChildren(...shown.map((account) => {
    const row = node('div', 'table-row');
    row.setAttribute('role', 'row');

    const result = node('span', `cell-result tone-${account.result}`, RESULTS[account.result] || account.result);
    if (account.reason) result.title = account.reason;

    const cells = [
      node('span', 'cell-muted', String(account.line)),
      node('span', 'cell-mono', account.label),
      node('span', '', String(account.characters)),
      result,
      node('span', '', account.jewels ? String(account.jewels) : '–'),
      node('span', 'cell-muted', account.seconds === null ? '–' : `${Math.round(account.seconds)}s`),
    ];
    cells.forEach((cell) => cell.setAttribute('role', 'cell'));
    row.append(...cells);
    return row;
  }));

  $('rows-empty').hidden = shown.length > 0;
  $('rows-empty').textContent = state.accounts.length ? 'No accounts match.' : 'No accounts yet';
  $('rows-note').textContent = state.accounts.length ? `Showing ${shown.length} of ${matches.length}, newest first` : '';
}

function renderLog(state) {
  $('log-empty').hidden = state.log.length > 0;
  $('log').replaceChildren(...state.log.map((entry) => {
    const line = node('div', 'log-line');
    line.append(node('span', 'log-time', clock(entry.ts)), node('span', `log-text tone-${entry.kind}`, entry.text));
    return line;
  }));
}

function renderRuns(state) {
  $('runs-empty').hidden = state.recent_runs.length > 0;
  $('runs').replaceChildren(...state.recent_runs.map((run) => {
    const row = node('div', 'run-row');
    const rate = run.seconds_per_claim === null ? '–' : run.seconds_per_claim.toFixed(1);
    const parts = [`${number(run.done)} of ${number(run.total)}`, duration(run.seconds), `${rate} s/claim`, run.source];
    if (run.status !== 'finished') parts.push(STATUS[run.status][0]);
    row.append(
      node('span', 'run-date', run.date),
      node('span', 'run-jewels', `${number(run.jewels)} jewels`),
      node('span', 'run-detail', parts.join(' · ')),
    );
    return row;
  }));
}

function render() {
  const state = view.latest;
  renderAlert(state);
  renderNetwork();
  renderGate();
  if (!state) return;

  renderHeader(state);
  renderKpis(state);
  renderProgress(state);
  renderChart(state);
  renderResults(state);
  renderAccounts(state);
  renderLog(state);
  renderRuns(state);
}

async function poll() {
  try {
    const response = await fetch('/api/state', { cache: 'no-store' });
    if (!response.ok) throw new Error('Bad response');
    const state = await response.json();
    if (state.server_id === serverId) {
      view.latest = state;
      view.offline = false;
    } else {
      reloadForNewServer();
    }
  } catch (error) {
    view.offline = true;
  }

  render();
  setTimeout(poll, POLL_MS);
}

function reloadForNewServer() {
  // The new server has a new token, which only a fresh page holds. A reload
  // repeats no POST, because they are all sent by fetch
  if (view.sending || view.reloading) return;
  view.reloading = true;

  sessionStorage.setItem('gow-concurrency', String(view.concurrency));
  if (view.gate || view.notice) sessionStorage.setItem('gow-notice', RESTART_NOTICE);
  window.location.reload();
}

function restoreAfterReload() {
  const concurrency = Number(sessionStorage.getItem('gow-concurrency'));
  if (CONCURRENCY_CHOICES.includes(concurrency)) view.concurrency = concurrency;

  view.notice = sessionStorage.getItem('gow-notice') || '';
  sessionStorage.removeItem('gow-concurrency');
  sessionStorage.removeItem('gow-notice');
}

async function send(path, body) {
  if (view.sending) return;
  view.sending = true;
  view.notice = '';
  try {
    const response = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Gow-Token': token },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      const refusal = await response.json();
      view.notice = refusal.error;
    }
  } catch (error) {
    view.notice = 'Dashboard not responding';
  }
  view.sending = false;
  render();
}

function gated(path, label) {
  // A warning only: the server would accept the request either way
  if (networkState() === 'ok') {
    send(path, { concurrency: view.concurrency });
    return;
  }

  view.gate = { path, label };
  render();
}

$('primary').addEventListener('click', () => {
  const { action } = $('primary').dataset;
  if (action === 'stop') send('/api/stop', {});
  if (action === 'start') gated('/api/run', 'Start anyway');
});

$('retry').addEventListener('click', () => {
  gated('/api/retry', 'Retry anyway');
});

$('gate-go').addEventListener('click', () => {
  if (!view.gate) return;
  const { path } = view.gate;
  view.gate = null;
  render();
  send(path, { concurrency: view.concurrency });
});

$('gate-cancel').addEventListener('click', () => {
  view.gate = null;
  render();
});

document.querySelectorAll('[data-concurrency]').forEach((button) => {
  button.addEventListener('click', () => {
    view.concurrency = Number(button.dataset.concurrency);
    document.querySelectorAll('[data-concurrency]').forEach((other) => {
      other.setAttribute('aria-pressed', String(other === button));
    });
    render();
  });
});

document.querySelectorAll('[data-filter]').forEach((button) => {
  button.addEventListener('click', () => {
    view.filter = button.dataset.filter;
    document.querySelectorAll('[data-filter]').forEach((other) => {
      other.setAttribute('aria-pressed', String(other === button));
    });
    render();
  });
});

$('search').addEventListener('input', (event) => {
  view.query = event.target.value;
  render();
});

restoreAfterReload();
poll();
