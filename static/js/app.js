const state = {
  latLng: { lat: 37.4221, lng: -122.0841 },
  radius: 250,
  ready: false,
};

const addressInput = document.getElementById('address-input');
const searchBtn = document.getElementById('search-btn');
const radiusInput = document.getElementById('radius-input');
const radiusValue = document.getElementById('radius-value');
const generateBtn = document.getElementById('generate-btn');
const previewBtn = document.getElementById('preview-btn');
const previewContainer = document.getElementById('preview');
const resultsSelect = document.getElementById('address-results');
const logContainer = document.getElementById('log');
const latDisplay = document.getElementById('lat-display');
const lngDisplay = document.getElementById('lng-display');
const healthPill = document.getElementById('health-status');

const map = L.map('map');
const tileLayer = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '&copy; <a href="https://www.openstreetmap.org/">OpenStreetMap</a> contributors',
});

tileLayer.addTo(map);
map.setView([state.latLng.lat, state.latLng.lng], 16);

const marker = L.marker([state.latLng.lat, state.latLng.lng], { draggable: true }).addTo(map);
const radiusCircle = L.circle([state.latLng.lat, state.latLng.lng], {
  radius: state.radius,
  color: '#2e7dff',
  fillColor: '#2e7dff',
  fillOpacity: 0.15,
}).addTo(map);

marker.on('move', (evt) => updateLatLng(evt.latlng));
map.on('click', (evt) => {
  marker.setLatLng(evt.latlng);
  updateLatLng(evt.latlng);
});

function updateLatLng(latlng) {
  state.latLng = { lat: latlng.lat, lng: latlng.lng };
  radiusCircle.setLatLng(latlng);
  latDisplay.textContent = latlng.lat.toFixed(5);
  lngDisplay.textContent = latlng.lng.toFixed(5);
}

function setRadius(value) {
  const intValue = parseInt(value, 10);
  state.radius = intValue;
  radiusValue.textContent = intValue;
  radiusCircle.setRadius(intValue);
}

radiusInput.addEventListener('input', (event) => setRadius(event.target.value));
searchBtn.addEventListener('click', handleSearch);
resultsSelect.addEventListener('change', () => {
  const option = resultsSelect.selectedOptions[0];
  if (!option) return;
  const lat = Number(option.dataset.lat);
  const lng = Number(option.dataset.lng);
  moveMap(lat, lng);
});

generateBtn.addEventListener('click', handleGenerate);
previewBtn.addEventListener('click', handlePreview);

async function handleSearch() {
  const query = addressInput.value.trim();
  if (!query) {
    pushLog('Enter an address to search.');
    return;
  }
  toggleBusy(true);
  try {
    const response = await fetch(`/api/geocode?query=${encodeURIComponent(query)}`);
    if (!response.ok) {
      const err = await response.json().catch(() => ({ detail: 'Nothing found for that search.' }));
      throw new Error(err.detail || 'Nothing found for that search.');
    }
    const data = await response.json();
    populateResults(data.results || []);
  } catch (error) {
    pushLog(error.message || 'Unable to reach the geocoder.');
  } finally {
    toggleBusy(false);
  }
}

function populateResults(results) {
  resultsSelect.innerHTML = '';
  if (!results.length) {
    pushLog('No matching addresses.');
    return;
  }
  const fragment = document.createDocumentFragment();
  results.forEach((result, index) => {
    const option = document.createElement('option');
    option.value = result.display_name;
    option.textContent = result.display_name;
    option.dataset.lat = result.latitude;
    option.dataset.lng = result.longitude;
    if (index === 0) {
      option.selected = true;
    }
    fragment.appendChild(option);
  });
  resultsSelect.appendChild(fragment);
  const head = results[0];
  moveMap(head.latitude, head.longitude);
  pushLog(`Showing ${results.length} match(es). Click the best fit.`);
}

function moveMap(lat, lng) {
  const latNum = Number(lat);
  const lngNum = Number(lng);
  map.setView([latNum, lngNum], 17);
  marker.setLatLng([latNum, lngNum]);
  radiusCircle.setLatLng([latNum, lngNum]);
  updateLatLng({ lat: latNum, lng: lngNum });
}

function buildPayload() {
  return {
    latitude: state.latLng.lat,
    longitude: state.latLng.lng,
    radius_meters: state.radius,
    highlight_home: true,
    formats: ['stl'],
  };
}

async function handleGenerate() {
  const payload = buildPayload();
  toggleBusy(true, 'Generating model…');
  try {
    const response = await fetch('/api/models', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      const err = await response.json().catch(() => ({ detail: 'Server error' }));
      throw new Error(err.detail || 'Model failed');
    }
    const blob = await response.blob();
    const url = window.URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `print3dhood_${payload.radius_meters}m_layers.zip`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.URL.revokeObjectURL(url);
    pushLog('Model generated. Check your downloads!');
  } catch (error) {
    pushLog(error.message || 'Could not generate the model.');
  } finally {
    toggleBusy(false);
  }
}

async function handlePreview() {
  const payload = buildPayload();
  togglePreviewBusy(true);
  renderPreviewMessage('Loading preview…');
  try {
    const response = await fetch('/api/models/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      const err = await response.json().catch(() => ({ detail: 'Preview unavailable' }));
      throw new Error(err.detail || 'Preview failed');
    }
    const data = await response.json();
    renderPreview(data);
    pushLog('Preview updated.');
  } catch (error) {
    renderPreviewMessage(error.message || 'Unable to load preview.');
    pushLog(error.message || 'Preview failed.');
  } finally {
    togglePreviewBusy(false);
  }
}

function renderPreview(data) {
  if (!data || !data.metadata || !Array.isArray(data.previews)) {
    renderPreviewMessage('No preview data available.');
    return;
  }
  const { metadata, previews } = data;
  const fragment = document.createDocumentFragment();
  const title = document.createElement('h3');
  title.textContent = `Layers preview (radius ${metadata.radius_meters} m)`;
  fragment.appendChild(title);

  const info = document.createElement('p');
  info.className = 'help-text';
  info.textContent = `${metadata.building_count ?? 0} buildings detected`;
  fragment.appendChild(info);

  const grid = document.createElement('div');
  grid.className = 'preview-grid';

  const order = ['water_layer', 'green_layer', 'building_layer', 'highlight_layer'];
  order
    .map((key) => previews.find((layer) => layer.name === key))
    .filter(Boolean)
    .forEach((layer) => {
      const card = document.createElement('div');
      card.className = 'preview-card';
      const name = document.createElement('h4');
      name.textContent = layer.name.replace('_', ' ');

      const canvas = document.createElement('canvas');
      canvas.width = 160;
      canvas.height = 160;
      drawLayerCanvas(canvas, layer);

      card.append(name, canvas);
      grid.appendChild(card);
    });

  fragment.appendChild(grid);
  previewContainer.replaceChildren(fragment);
}

function renderPreviewMessage(message) {
  previewContainer.textContent = message;
}

function drawLayerCanvas(canvas, layer) {
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (layer.name === 'highlight_layer') {
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
  }

  drawPaths(ctx, layer.base_paths, layer.base_color || '#e5e7eb');
  drawPaths(ctx, layer.feature_paths, layer.feature_color || '#1f2937');
  drawPaths(ctx, layer.overlay_paths, layer.overlay_color || null);
}

function drawPaths(ctx, paths, color) {
  if (!color || !paths || !paths.length) return;
  ctx.save();
  ctx.fillStyle = color;
  ctx.globalAlpha = 0.95;

  paths.forEach((path) => {
    ctx.beginPath();
    path.outer.forEach(([nx, ny], idx) => {
      const x = nx * ctx.canvas.width;
      const y = ny * ctx.canvas.height;
      if (idx === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.closePath();
    ctx.fill();

    if (path.holes && path.holes.length) {
      ctx.save();
      ctx.fillStyle = '#ffffff';
      path.holes.forEach((hole) => {
        ctx.beginPath();
        hole.forEach(([nx, ny], idx) => {
          const x = nx * ctx.canvas.width;
          const y = ny * ctx.canvas.height;
          if (idx === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        });
        ctx.closePath();
        ctx.fill();
      });
      ctx.restore();
    }
  });

  ctx.restore();
}

function pushLog(message) {
  const timestamp = new Date().toLocaleTimeString();
  const entry = document.createElement('div');
  entry.textContent = `[${timestamp}] ${message}`;
  logContainer.prepend(entry);
}

function toggleBusy(busy, label) {
  generateBtn.disabled = busy;
  searchBtn.disabled = busy;
  generateBtn.textContent = busy ? label || 'Working…' : 'Generate printable model';
}

function togglePreviewBusy(busy) {
  previewBtn.disabled = busy;
  previewBtn.textContent = busy ? 'Loading preview…' : 'Preview layers';
}

async function checkHealth() {
  try {
    const response = await fetch('/api/health');
    if (!response.ok) {
      throw new Error('Offline');
    }
    const data = await response.json();
    healthPill.textContent = `${data.service || 'API'} online`;
    healthPill.classList.add('ok');
    state.ready = true;
  } catch (error) {
    healthPill.textContent = 'Backend unreachable';
    healthPill.classList.remove('ok');
  }
}

checkHealth();
setRadius(radiusInput.value);
pushLog('Click anywhere on the map to select your home.');
renderPreviewMessage('Preview the four layers before downloading.');
