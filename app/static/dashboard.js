// Dashboard client: polls /api/stats every 5s and re-renders.
const fmt = new Intl.NumberFormat('en', {maximumFractionDigits: 2});

function meter(label, used, total, pct, unit) {
  const tone = pct > 90 ? 'bg-rose-500' : pct > 70 ? 'bg-amber-500' : 'bg-emerald-500';
  return `<div class="rounded-lg border border-slate-800 bg-slate-900/50 p-4">
    <div class="flex justify-between items-baseline">
      <span class="text-sm text-slate-300">${label}</span>
      <span class="text-xs text-slate-500">${pct}%</span>
    </div>
    <div class="mt-2 h-2 w-full rounded bg-slate-800 overflow-hidden">
      <div class="h-full ${tone} transition-all duration-500" style="width:${Math.min(pct,100)}%"></div>
    </div>
    <div class="mt-2 text-xs text-slate-400">${fmt.format(used)} / ${fmt.format(total)} ${unit}</div>
  </div>`;
}

function badge(text) {
  const map = {
    ACTIVE: 'bg-emerald-900/60 text-emerald-300', ONLINE: 'bg-emerald-900/60 text-emerald-300',
    available: 'bg-emerald-900/60 text-emerald-300',
    BUILD: 'bg-sky-900/60 text-sky-300', creating: 'bg-sky-900/60 text-sky-300',
    PENDING_CREATE: 'bg-sky-900/60 text-sky-300',
    SHUTOFF: 'bg-slate-800 text-slate-300', OFFLINE: 'bg-slate-800 text-slate-300',
    'in-use': 'bg-indigo-900/60 text-indigo-300',
    SHELVED_OFFLOADED: 'bg-violet-900/60 text-violet-300',
    ERROR: 'bg-rose-900/60 text-rose-300'
  };
  const tone = map[text] || 'bg-slate-800 text-slate-300';
  return `<span class="px-2 py-0.5 rounded text-xs ${tone}">${text}</span>`;
}

function counter(label, value) {
  return `<div class="rounded-lg border border-slate-800 bg-slate-900/50 px-3 py-2">
    <div class="text-lg font-semibold text-white">${value}</div>
    <div class="text-xs text-slate-400">${label}</div></div>`;
}

async function refresh() {
  let data;
  try { data = await (await fetch('/api/stats')).json(); }
  catch (e) { document.getElementById('updated').textContent = 'unreachable'; return; }

  const h = data.host;
  document.getElementById('host-line').textContent =
    `${h.host} · ${h.vcpus_total} threads · ${fmt.format(h.ram_total_mb/1024)} GB RAM · ` +
    `${fmt.format(h.disk_total_gb/1024)} TB disk · overcommit ${data.config.cpu_allocation_ratio}x`;
  document.getElementById('updated').textContent = data.generated_at;
  // Every environment serves on the same ports, so the file name is the only thing on
  // the page that says which one you are looking at.
  document.getElementById('database').textContent = data.config.database;

  document.getElementById('meters').innerHTML =
    meter('vCPU', h.vcpus_used, h.vcpus_allocatable, h.vcpus_pct, 'vCPU (overcommitted)') +
    meter('RAM', h.ram_used_mb/1024, h.ram_allocatable_mb/1024, h.ram_pct, 'GB (+256MB/VM)') +
    meter('Disk', h.disk_used_gb, h.disk_allocatable_gb, h.disk_pct, 'GB (instances + volumes)') +
    meter('Conntrack', h.conntrack_used, h.conntrack_max, h.conntrack_pct, 'entries');

  const c = data.counts;
  document.getElementById('counts').innerHTML =
    counter('instances', h.total_instances) + counter('running', h.running_vms) +
    counter('networks', c.networks) + counter('images', c.images) +
    counter('sec groups', c.security_groups) + counter('floating IPs', c.floating_ips) +
    counter('objects', c.objects);

  const wrap = document.getElementById('scenario-wrap');
  wrap.classList.toggle('hidden', data.scenarios.length === 0);
  document.getElementById('scenarios').innerHTML = data.scenarios.map(s =>
    `<div class="px-3 py-2 text-sm flex justify-between">
       <span class="text-amber-200">${s.service} → ${s.action}</span>
       <span class="text-amber-400/70 text-xs">${s.hits} hits · ${s.remaining_seconds}s left</span>
     </div>`).join('');

  document.getElementById('servers').innerHTML = data.servers.map(s =>
    `<tr class="hover:bg-slate-900/60">
      <td class="px-3 py-2 text-slate-200">${s.name}</td>
      <td class="px-3 py-2">${badge(s.status)}${s.task_state ? `<span class="ml-1 text-xs text-slate-500">${s.task_state}</span>` : ''}</td>
      <td class="px-3 py-2 text-slate-400">${s.flavor}</td>
      <td class="px-3 py-2 text-right text-slate-400">${s.vcpus}</td>
      <td class="px-3 py-2 text-right text-slate-400">${fmt.format(s.ram_mb)}M</td>
      <td class="px-3 py-2 text-slate-400 font-mono text-xs">${s.ip || '—'}</td>
    </tr>`).join('') ||
    '<tr><td colspan="6" class="px-3 py-6 text-center text-slate-600">no instances</td></tr>';

  document.getElementById('volumes').innerHTML = data.volumes.map(v =>
    `<tr class="hover:bg-slate-900/60">
      <td class="px-3 py-2 text-slate-200">${v.name || v.id.slice(0,8)}</td>
      <td class="px-3 py-2">${badge(v.status)}</td>
      <td class="px-3 py-2 text-right text-slate-400">${v.size}</td>
      <td class="px-3 py-2 text-slate-400">${v.type}</td>
    </tr>`).join('') ||
    '<tr><td colspan="4" class="px-3 py-6 text-center text-slate-600">no volumes</td></tr>';

  document.getElementById('lbs').innerHTML = data.loadbalancers.map(l =>
    `<tr class="hover:bg-slate-900/60">
      <td class="px-3 py-2 text-slate-200">${l.name || l.id.slice(0,8)}</td>
      <td class="px-3 py-2">${badge(l.provisioning_status)}</td>
      <td class="px-3 py-2">${badge(l.operating_status)}</td>
      <td class="px-3 py-2 text-slate-400 font-mono text-xs">${l.vip || '—'}</td>
    </tr>`).join('') ||
    '<tr><td colspan="4" class="px-3 py-6 text-center text-slate-600">no load balancers</td></tr>';

  document.getElementById('billing').innerHTML =
    data.billing.lines.map(l =>
      `<div class="px-3 py-2 flex justify-between text-sm">
        <span class="text-slate-300">${l.res_type}</span>
        <span class="text-slate-400">${fmt.format(l.qty)} units · <span class="text-emerald-300">$${l.rate.toFixed(4)}</span></span>
      </div>`).join('') +
    `<div class="px-3 py-2 flex justify-between text-sm bg-slate-900/60">
       <span class="text-white font-medium">total</span>
       <span class="text-emerald-300 font-medium">$${data.billing.total.toFixed(4)}</span></div>`;
}

refresh();
setInterval(refresh, 5000);
