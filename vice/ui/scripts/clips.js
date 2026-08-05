'use strict';
// clips.js — clip grid + cards + actions (trigger/delete/share/rename)

// ═══════════════════════════════════════════════════════════════════
// Clips — fetch + render
// ═══════════════════════════════════════════════════════════════════
async function fetchClips() {
  try {
    const r = await fetch('/api/clips');
    const d = await r.json();
    clips = d.clips || [];
    renderClips();
    renderHomeRecent();
    renderMostViewed();
    renderStats();
    renderPlaylists();
    if (typeof edOnClipsRefreshed === 'function') edOnClipsRefreshed();
  } catch (_) {
    document.getElementById('clip-sub').textContent = 'Cannot reach daemon';
  }
}

let activePreviewVideo = null;
function stopActivePreview(resetTime = true) {
  if (!activePreviewVideo) return;
  try {
    activePreviewVideo.pause();
    if (resetTime) activePreviewVideo.currentTime = 0;
  } catch (_) {}
  const card = activePreviewVideo.closest('.clip-card');
  if (card) card.classList.remove('preview-on');
  activePreviewVideo = null;
}
// Hover previews resolve from the hovered element, not the slug: card ids
// are duplicated across the grid and both home rows, so an id lookup could
// start the preview on a different card than the one under the pointer.
function startPreview(el) {
  const card = el.closest('.clip-card');
  const vid = card && card.querySelector('video.preview-video');
  if (!vid) return;
  if (activePreviewVideo && activePreviewVideo !== vid) stopActivePreview(true);
  card.classList.add('preview-on');
  activePreviewVideo = vid;
  const maybe = vid.play();
  if (maybe && typeof maybe.catch === 'function') maybe.catch(() => {});
}
function stopPreview(el) {
  const card = el.closest('.clip-card');
  const vid = card && card.querySelector('video.preview-video');
  if (!vid) return;
  card.classList.remove('preview-on');
  try { vid.pause(); vid.currentTime = 0; } catch (_) {}
  if (activePreviewVideo === vid) activePreviewVideo = null;
}

function currentPlaylist() {
  return currentPlaylistId ? playlists.find(p => p.id === currentPlaylistId) : null;
}

// The clips view doubles as the playlist detail view: membership first,
// then the search filter over name + game.
function visibleClips() {
  let list = clips;
  const pl = currentPlaylist();
  if (pl) list = list.filter(c => pl.clip_slugs.includes(c.slug));
  const q = searchQuery.trim().toLowerCase();
  if (q) list = list.filter(c => `${c.name} ${c.game || ''}`.toLowerCase().includes(q));
  return list;
}

function renderClips() {
  const grid  = document.getElementById('clips-grid');
  const empty = document.getElementById('clips-empty');
  const sub   = document.getElementById('clip-sub');
  const dir   = cfg.output?.directory || '~/Videos/Vice';

  if (currentPlaylistId && !currentPlaylist()) {
    currentPlaylistId = null;
    updateSidebarActive();
  }
  const pl = currentPlaylist();
  const list = visibleClips();
  const q = searchQuery.trim();
  const n = list.length;
  const clipsWord = `${n} clip${n !== 1 ? 's' : ''}`;

  setText('clips-title', pl ? pl.name : 'All Clips');
  sub.textContent = pl
    ? `${clipsWord} · ${pl.kind === 'custom' ? 'custom playlist' : 'auto playlist from game detection'}`
    : (n === 0 && !q ? 'No clips saved yet' : `${clipsWord} in ${dir}${q ? ` matching "${q}"` : ''}`);
  renderPlaylistHeader(pl);

  const emptyTitle = empty.querySelector('h3');
  if (q && n === 0) {
    emptyTitle.textContent = 'No clips match';
    document.getElementById('empty-hint').textContent = 'Try a different search.';
  } else if (pl && n === 0) {
    emptyTitle.textContent = 'This playlist is empty';
    document.getElementById('empty-hint').textContent = pl.kind === 'custom'
      ? 'Add clips from the + button on any clip card.'
      : 'New clips land here automatically when this game is detected.';
  } else {
    emptyTitle.textContent = 'The reel is empty';
    syncDynamicCopy();
  }
  empty.style.display = n === 0 ? 'block' : 'none';
  grid.style.display  = n === 0 ? 'none' : 'grid';
  stopActivePreview(true);

  grid.innerHTML = list.map(c => cardHTML(c)).join('');
  attachPreviewFailureHandlers(grid);
}

// Hover previews that cannot decode fall back to the thumbnail quietly.
// Media events fire only on the <video> itself (no capture/bubble path to
// ancestors), so handlers go on each element after every render. Missing
// codecs do not fire `error` at all: the video track is silently dropped
// and videoWidth stays 0 (issue #79).
const previewErrLogged = new Set();
function attachPreviewFailureHandlers(grid) {
  grid.querySelectorAll('video.preview-video').forEach(vid => {
    const fail = why => {
      const card = vid.closest('.clip-card');
      const slug = card ? card.id.replace(/^card-/, '') : '?';
      if (card) card.classList.remove('preview-on');
      if (!previewErrLogged.has(slug)) {
        previewErrLogged.add(slug);
        nativeLog(`video error: preview ${slug} ${why} h264=${H264_SUPPORTED}`);
      }
    };
    vid.addEventListener('error', () => {
      if (vid.getAttribute('src')) fail(mediaErrorName(vid.error));
    });
    vid.addEventListener('loadeddata', () => {
      if (vid.videoWidth === 0) fail('NO_VIDEO_TRACK');
    });
  });
}

function cardHTML(c) {
  const sizeStr = c.size     ? `${(c.size / 1048576).toFixed(1)} MB` : '';
  const resStr  = c.width    ? `${c.width}×${c.height}`         : '';
  const durStr  = c.duration ? fmtSec(Math.round(c.duration), true)  : '';
  const dateStr = c.created_at
    ? new Date(c.created_at).toLocaleDateString(undefined, {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})
    : '';
  const isNew = recentNew.has(c.slug);
  const slug  = escAttr(c.slug);
  const arg   = escArg(c.slug);
  const name  = escHtml(c.name || c.slug);
  const viewsStr = c.views ? `${c.views} view${c.views !== 1 ? 's' : ''}` : '';
  const meta  = [dateStr, resStr, sizeStr, viewsStr].filter(Boolean).join(' · ');

  // Cards always use the tiny persistent 15-second tail preview. They never hand a
  // full MP4 or browser-hostile MKV to the lightweight hover player.
  const hoverPreview = !!c.preview_url;
  const hoverHandlers = hoverPreview
    ? 'onpointerenter="startPreview(this)" onpointerleave="stopPreview(this)"'
    : '';
  const mediaHtml = c.thumb_url
    ? `<img src="${escAttr(c.thumb_url)}" loading="lazy" alt="" draggable="false">
       ${hoverPreview
         ? `<video class="preview-video" src="${escAttr(c.preview_url)}" muted loop playsinline preload="none"></video>`
         : ''}`
    : `<div class="thumb-placeholder">${svgEl('film', 32)}</div>`;

  const shareDisabled = !c.share_url;
  const shareBtn = `<button class="clip-icon-btn" title="${shareDisabled ? 'No share URL yet' : 'Copy share link'}" ${shareDisabled ? 'disabled' : `onclick="copyLink(event, '${escArg(c.share_url)}', ${c.share_is_public !== false})"`}>${svgEl('link2', 12)}</button>`;

  return `
  <div class="clip-card" id="card-${slug}" draggable="true"
       ondragstart="onClipDragStart(event, '${arg}')" ondragend="onClipDragEnd(event)"
       oncontextmenu="openPlaylistMenu(event, '${arg}')">
    <div class="thumb-wrap" onclick="openViewer('${arg}')" ${hoverHandlers}>
      ${mediaHtml}
      <div class="thumb-play-overlay">${svgEl('play', 38)}</div>
      ${durStr ? `<div class="clip-dur-badge mono">${durStr}</div>` : ''}
      ${isNew  ? `<div class="clip-new-badge mono">NEW</div>`       : ''}
    </div>
    <div class="clip-body">
      <div class="clip-copy">
        <div class="clip-name" title="${escAttr(c.name || c.slug)} — double-click to rename" ondblclick="startRename('${arg}', this)">${name}</div>
        <div class="clip-meta mono">${escHtml(meta)}</div>
      </div>
      <div class="clip-actions">
        <button class="clip-icon-btn" title="Trim" onclick="openTrim('${arg}', '${escArg(c.video_url || '')}')">${svgEl('scissors', 12)}</button>
        <button class="clip-icon-btn" title="Copy video to clipboard" onclick="copyClipFile(event, '${arg}')">${svgEl('clipboard', 12)}</button>
        ${shareBtn}
        <button class="clip-icon-btn" title="Reveal in file manager" onclick="revealClip('${arg}')">${svgEl('folderOpen', 12)}</button>
        <button class="clip-icon-btn danger" title="Delete" onclick="delClip('${arg}')">${svgEl('trash2', 12)}</button>
      </div>
    </div>
  </div>`;
}

async function triggerClip() {
  try {
    await fetch('/api/trigger', { method: 'POST' });
  } catch (_) { toast('Daemon not running', 'err'); }
}

async function delClip(slug) {
  if (!confirm('Delete this clip? This cannot be undone.')) return;
  await performDeleteClip(slug);
}

async function performDeleteClip(slug) {
  try {
    await fetch(`/api/clips/${encodeURIComponent(slug)}`, { method: 'DELETE' });
    recentNew.delete(slug);
    clips = clips.filter(c => c.slug !== slug);
    renderClips();
    renderHomeRecent();
    renderMostViewed();
    renderStats();
    toast('Clip deleted', 'ok');
  } catch (_) { toast('Failed to delete', 'err'); }
}

function copyLink(ev, url, isPublic) {
  if (ev) { ev.preventDefault(); ev.stopPropagation(); }
  nativeLog(`copyLink: url=${(url || '').slice(0, 60)}`);
  if (!url) return;
  copyToClipboard(url).then(ok => {
    if (!ok) { showManualCopyModal(url); return; }
    // A LAN address looks identical to a real share link until a friend
    // tries to open it and cannot (#105).
    if (isPublic === false) {
      toast('Link copied, but it only works on your network. Install cloudflared for public links.', 'warn');
    } else {
      toast('Share link copied!', 'ok');
    }
  });
}

async function copyClipFile(ev, slug) {
  if (ev) { ev.preventDefault(); ev.stopPropagation(); }
  if (!slug) return;
  try {
    const r = await fetch(`/api/clips/${encodeURIComponent(slug)}/copy-file`, { method: 'POST' });
    const d = await r.json();
    if (d.ok) toast('Video copied, paste it anywhere', 'ok');
    else toast(d.error || 'Could not copy the video', 'err');
  } catch (_) { toast('Could not copy the video', 'err'); }
}

async function revealClip(slug) {
  try {
    await fetch(`/api/clips/${encodeURIComponent(slug)}/reveal`, { method: 'POST' });
  } catch (_) { toast('Could not open file manager', 'err'); }
}

// Playback fallback for engines that cannot decode the clip in-app:
// hand the file to the system default video player.
async function openClipExternally(slug) {
  if (!slug) return;
  try {
    const r = await fetch(`/api/clips/${encodeURIComponent(slug)}/open`, { method: 'POST' });
    if (!r.ok) throw new Error(String(r.status));
    toast('Opened in system player', 'ok');
  } catch (_) { toast('Could not open system player', 'err'); }
}

// `el` is the element the user clicked. Home recent cards share ids with
// grid cards, so getElementById would resolve to the hidden home card and
// swap the rename input into a view the user cannot see (issue #99). The
// clicked element pins the rename to the card actually on screen.
function startRename(slug, el) {
  const card = el ? el.closest('.clip-card') : document.getElementById('card-' + slug);
  if (!card) return;
  const nameEl = card.querySelector('.clip-name');
  if (!nameEl) return; // rename already in progress on this card
  const current = nameEl.textContent.trim().replace(/\.mp4$/i, '');
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'clip-rename-input';
  input.value = current;
  nameEl.replaceWith(input);
  card.classList.add('renaming');
  input.focus(); input.select();
  let submitted = false;
  const revert = () => { card.classList.remove('renaming'); input.replaceWith(nameEl); };
  const submit = async () => {
    if (submitted) return;
    submitted = true;
    const v = input.value.trim();
    if (!v || v === current) { revert(); return; }
    try {
      const r = await fetch(`/api/clips/${encodeURIComponent(slug)}/rename`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: v }),
      });
      const data = await r.json();
      if (!data.ok) { toast(data.error || 'Rename failed', 'err'); revert(); }
      else {
        card.classList.remove('renaming');
        // Spaces and punctuation are normalised server-side, so say what
        // actually landed on disk when it differs from what was typed.
        if (data.slug && data.slug !== v) toast(`Saved as ${data.name || data.slug}`);
      }
    } catch (_) { toast('Rename failed', 'err'); revert(); }
  };
  const cancel = () => { if (!submitted) { submitted = true; revert(); } };
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter')  { e.preventDefault(); submit(); }
    if (e.key === 'Escape') { cancel(); }
  });
  input.addEventListener('blur', submit);
}
