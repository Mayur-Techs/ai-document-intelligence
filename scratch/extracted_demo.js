
let selectedType = 'invoice';
let docs = [];
let selectedDocId = null;
let isLoggedIn = false;

// Silently check auth state on page load
(async function() {
  try {
    const authRes = await fetch('https://doc-intelligence-api-tubh.onrender.com/auth/me', {
      credentials: 'include'
    });
    isLoggedIn = authRes.ok;
  } catch (e) {
    isLoggedIn = false;
  }
  // Re-render if there's a selected document to reveal/blur the results
  renderResults();
})();

// ── Toast ────────────────────────────────────────────────────────────────────
function showToast(msg, type) {
  const container = document.getElementById('toastContainer');
  const t = document.createElement('div');
  t.className = 'toast ' + (type || 'success');
  t.textContent = msg;
  container.appendChild(t);
  setTimeout(() => { t.remove(); }, 3500);
}

// ── Type selector ────────────────────────────────────────────────────────────
document.querySelectorAll('.type-btn').forEach(function(btn) {
  btn.addEventListener('click', function() {
    document.querySelectorAll('.type-btn').forEach(function(b) { b.classList.remove('active'); });
    btn.classList.add('active');
    selectedType = btn.dataset.type;
  });
});

// ── Drag and drop ────────────────────────────────────────────────────────────
var zone  = document.getElementById('uploadZone');
var input = document.getElementById('fileInput');

zone.addEventListener('dragover', function(e) { e.preventDefault(); zone.classList.add('drag'); });
zone.addEventListener('dragleave', function() { zone.classList.remove('drag'); });
zone.addEventListener('drop', function(e) {
  e.preventDefault();
  zone.classList.remove('drag');
  Array.from(e.dataTransfer.files)
    .filter(function(f) { return f.type === 'application/pdf'; })
    .forEach(uploadFile);
});
zone.addEventListener('click', function() { input.click(); });

input.addEventListener('change', function() {
  Array.from(input.files).forEach(uploadFile);
  input.value = '';
});

// ── Upload ───────────────────────────────────────────────────────────────────
function uploadFile(file) {
  var doc = {
    localId: Date.now() + Math.random(),
    name: file.name,
    size: file.size,
    type: selectedType,
    status: 'queued',
    progress: 0,
    data: null,
    docId: null,
    error: null,
  };

  docs.unshift(doc);
  selectedDocId = doc.localId;
  renderQueue();
  renderResults();

  // Try real API first (this helper uses credentials: include)
  (typeof DocAPI !== 'undefined' && DocAPI.uploadDocument
    ? DocAPI.uploadDocument(file, selectedType)
    : Promise.reject('no api')
  ).then(function(uploaded) {
    doc.docId = uploaded.document_id || uploaded.id;
    doc.status = 'processing';
    doc.progress = 25;
    renderQueue();

    return pollUntilDone(doc);

  }).catch(function(err) {
    var msg = (err && err.message) || '';
    var status = (err && err.status) || 0;

    if (err.name === 'AbortError') {
      // API took too long — show message, don't simulate
      doc.status = 'failed';
      doc.error = 'API is waking up. Please wait 30 seconds and try again.';
      renderQueue();
      renderResults();
      showToast(doc.error, 'error');
    } else if (status >= 400) {
      doc.status = 'failed';
      if (status === 401) {
        doc.error = 'Authentication required. Please sign up or log in to upload documents.';
      } else if (status === 429) {
        doc.error = msg || 'Daily upload limit reached. Sign up for a higher quota.';
      } else {
        doc.error = msg || 'Server error (' + status + ')';
      }
      renderQueue();
      renderResults();
      showToast(doc.error, 'error');
    } else {
      // Truly unreachable — simulate as fallback
      simulateExtraction(doc);
    }
  });
}

function pollUntilDone(doc) {
  if (typeof DocAPI !== 'undefined' && DocAPI.pollDocument) {
    return DocAPI.pollDocument(doc.docId, function(interim) {
      doc.status = interim.status;
      doc.progress = interim.status === 'processing' ? 60 : 90;
      renderQueue();
    }).then(function(result) {
      doc.status = result.status;
      doc.progress = 100;
      doc.data = result;
      if (result.status === 'failed') doc.error = result.error_message;
      renderQueue();
      renderResults();
      showToast(doc.name.substring(0,30) + ' — extraction complete', 'success');
    });
  }

  // Fallback polling via fetch with credentials
  return new Promise(function(resolve) {
    var attempts = 0;
    var timer = setInterval(function() {
      attempts++;
      doc.progress = Math.min(90, 25 + attempts * 10);
      renderQueue();

      fetch(API_BASE + '/documents/' + doc.docId, { credentials: 'include' })
        .then(function(r) { return r.json(); })
        .then(function(result) {
          if (result.status === 'completed' || result.status === 'needs_review' || result.status === 'failed' || attempts > 20) {
            clearInterval(timer);
            doc.status = result.status;
            doc.progress = 100;
            doc.data = result;
            if (result.status === 'failed') doc.error = result.error_message;
            renderQueue();
            renderResults();
            showToast(doc.name.substring(0,30) + ' — done', 'success');
            resolve();
          }
        }).catch(function() {
          if (attempts > 20) { clearInterval(timer); resolve(); }
        });
    }, 2000);
  });
}

// ── Simulate ─────────────────────────────────────────────────────────────────
function simulateExtraction(doc) {
  doc.status = 'processing'; doc.progress = 25; renderQueue(); renderResults();
  setTimeout(function() { doc.progress = 55; renderQueue(); renderResults(); }, 1200);
  setTimeout(function() { doc.progress = 85; renderQueue(); renderResults(); }, 2200);
  setTimeout(function() {
    doc.status = 'completed'; doc.progress = 100;
    doc.data = {
      status: 'completed',
      file_name: doc.name,
      page_count: 2,
      ai_confidence: 0.91,
      vendor_name: 'Sharma Freight Solutions Pvt Ltd',
      invoice_number: 'INV-2026-04892',
      invoice_date: '15 Apr 2026',
      due_date: '15 May 2026',
      currency: 'INR',
      vendor_gstin: '27AAPFU0939F1ZV',
      buyer_gstin: '27AABCU9603R1ZM',
      buyer_name: 'Tech Innovations Corp',
      subtotal: 167250,
      tax_amount: null,
      total_amount: 167250,
      fields: [
        { field_name: 'line_items_1', field_value: JSON.stringify({ description: 'Ocean Freight Mumbai → Rotterdam', quantity: 1, unit_price: 142500, amount: 142500 }) },
        { field_name: 'line_items_2', field_value: JSON.stringify({ description: 'Port Handling Charges', amount: 8250 }) },
        { field_name: 'line_items_3', field_value: JSON.stringify({ description: 'Custom Clearance Documentation', amount: 4500 }) },
        { field_name: 'line_items_4', field_value: JSON.stringify({ description: 'Inland Transport — Factory to Port', amount: 12000 }) },
      ],
      _simulated: true,
    };
    renderQueue();
    renderResults();
    showToast('Demo extraction complete (simulated)', 'success');
  }, 3200);
}

// ── Queue renderer ───────────────────────────────────────────────────────────
function renderQueue() {
  var el = document.getElementById('queueList');
  var badge = document.getElementById('queueCount');
  if (!docs.length) {
    el.innerHTML = '<div style="text-align:center;padding:2rem 0;color:var(--text-700);font-size:12px;">No documents yet</div>';
    badge.style.display = 'none';
    return;
  }
  badge.style.display = 'inline-flex';
  badge.textContent = docs.length;
  el.innerHTML = docs.map(function(d) {
    var label = { queued: 'Queued', processing: 'Extracting...', completed: 'Done', failed: 'Failed' }[d.status] || d.status;
    return '<div class="queue-item ' + (d.localId === selectedDocId ? 'selected' : '') + '" onclick="selectDoc(\'' + d.localId + '\')">'
      + '<div class="queue-item-name" title="' + d.name + '">' + d.name + '</div>'
      + '<div class="queue-progress"><div class="queue-progress-fill" style="width:' + d.progress + '%"></div></div>'
      + '<div class="queue-item-meta">'
      + '<span style="font-size:10px;color:var(--text-700);">' + (d.size/1024).toFixed(1) + ' KB · ' + d.type + '</span>'
      + '<span class="status-chip ' + d.status + '">' + label + '</span>'
      + '</div></div>';
  }).join('');
}

function selectDoc(localId) {
  selectedDocId = localId;
  renderQueue();
  renderResults();
}

// ── Results renderer ─────────────────────────────────────────────────────────
function renderResults() {
  var el = document.getElementById('resultsArea');
  var doc = docs.find(function(d) { return d.localId == selectedDocId; });

  if (!doc) {
    el.innerHTML = '<div class="empty"><div class="empty-icon">📂</div><h3>Upload a PDF to begin</h3><p>Results appear here as each document is processed.</p></div>';
    return;
  }
  if (doc.status === 'queued') {
    el.innerHTML = '<div class="extracting"><div class="spinner"></div><p style="color:var(--text-400);font-size:14px;">Queued for processing...</p></div>';
    return;
  }
  if (doc.status === 'processing') {
    el.innerHTML = '<div class="extracting">'
      + '<div class="spinner"></div>'
      + '<p style="color:var(--text-400);font-size:14px;margin-bottom:0.5rem;">Extracting document...</p>'
      + '<p style="font-size:12px;color:var(--text-600);">' + doc.name + '</p>'
      + '<div class="extracting-steps">'
      + '<div class="extracting-step done">✓ PDF parsed with pdfplumber</div>'
      + '<div class="extracting-step ' + (doc.progress >= 60 ? 'done' : 'active') + '">' + (doc.progress >= 60 ? '✓' : '⟳') + ' Cerebras AI extraction</div>'
      + '<div class="extracting-step ' + (doc.progress >= 85 ? 'active' : '') + '">' + (doc.progress >= 85 ? '⟳' : '◦') + ' Groq fallback if needed</div>'
      + '</div></div>';
    return;
  }
  if (doc.status === 'failed') {
    el.innerHTML = '<div class="extracting"><div style="font-size:2rem;margin-bottom:1rem;">❌</div>'
      + '<p style="color:var(--red-500);">Extraction failed</p>'
      + '<p style="font-size:12px;color:var(--text-600);margin-top:0.5rem;">' + (doc.error || 'Unknown error — try a different PDF') + '</p></div>';
    return;
  }

  var d = doc.data;
  var conf = Math.round((d.ai_confidence || 0.9) * 100);
  var confColor = conf >= 85 ? 'var(--green-500)' : conf >= 60 ? 'var(--gold-500)' : 'var(--red-500)';

  var lineItems = (d.fields || [])
    .filter(function(f) { return f.field_name.startsWith('line_items_'); })
    .map(function(f) { try { return JSON.parse(f.field_value); } catch(e) { return { description: f.field_value }; } })
    .filter(Boolean);

  function fmt(n) { return n ? Number(n).toLocaleString('en-IN', { maximumFractionDigits: 0 }) : '—'; }
  function fmtAmt(n) { return n ? '₹' + fmt(n) : '—'; }
  function fv(v) { return v || null; }

  var lineItemsHtml = '';
  if (lineItems.length) {
    lineItemsHtml = '<div class="fields-section">'
      + '<div class="fields-title">Line Items (' + lineItems.length + ')</div>'
      + '<div class="line-items-table">'
      + '<div class="line-items-head"><span>Description</span><span>Qty</span><span>Unit Price</span><span>Amount</span></div>'
      + lineItems.map(function(li) {
          return '<div class="line-items-row">'
            + '<span>' + (li.description || '—') + '</span>'
            + '<span style="text-align:right;">' + (li.quantity != null ? li.quantity : '—') + '</span>'
            + '<span style="text-align:right;color:var(--text-400);">' + (li.unit_price ? '₹' + fmt(li.unit_price) : '—') + '</span>'
            + '<span style="text-align:right;color:var(--green-500);font-weight:500;">' + (li.amount ? '₹' + fmt(li.amount) : '—') + '</span>'
            + '</div>';
        }).join('')
      + '</div></div>';
  }

  var isLive = !d._simulated;
  var exportId = doc.docId;

  var bodyStyle = '';
  var overlayHtml = '';

  el.innerHTML = '<div class="result-card" style="position: relative;">'

    // Header
    + '<div class="result-header">'
    + '<div class="result-file">'
    + '<div class="result-file-icon">📄</div>'
    + '<div>'
    + '<div class="result-file-name">' + (d.file_name || doc.name) + '</div>'
    + '<div class="result-file-meta">'
    + (d.page_count ? '<span>' + d.page_count + ' pages</span>' : '')
    + '<span>' + (d.document_type || doc.type) + '</span>'
    + (d._simulated ? '<span style="color:var(--gold-500);">⚡ Demo data</span>' : '<span style="color:var(--green-500);">✓ Live extraction</span>')
    + '</div></div></div>'
    + '<div class="result-badges">'
    + '<div class="conf-circle" style="border-color:' + confColor + ';">'
    + '<div class="conf-val" style="color:' + confColor + ';">' + conf + '%</div>'
    + '<div class="conf-lbl">conf.</div>'
    + '</div></div></div>'

    // Body container
    + '<div class="result-body-container" style="position: relative;">'
    + '<div class="result-body"' + bodyStyle + '>'

    // Key fields
    + '<div class="fields-section">'
    + '<div class="fields-title">Extracted Fields</div>'
    + '<div class="fields-grid">'
    + fieldBox('Vendor Name', fv(d.vendor_name))
    + fieldBox('Buyer Name', fv(d.buyer_name))
    + fieldBox('Invoice Number', fv(d.invoice_number))
    + fieldBox('Invoice Date', fv(d.invoice_date))
    + fieldBox('Due Date', fv(d.due_date))
    + fieldBox('Currency', fv(d.currency))
    + fieldBox('Vendor GSTIN', fv(d.vendor_gstin), 'gstin')
    + fieldBox('Buyer GSTIN', fv(d.buyer_gstin), 'gstin')
    + fieldBox('Bank IFSC', fv(d.bank_ifsc))
    + '</div></div>'

    // Amounts
    + '<div class="amounts-row">'
    + '<div class="amount-box"><div class="amount-box-label">Subtotal</div><div class="amount-box-val" style="color:var(--text-300);">' + fmtAmt(d.subtotal) + '</div></div>'
    + '<div class="amount-box"><div class="amount-box-label">GST / Tax</div><div class="amount-box-val" style="color:var(--gold-500);">' + (d.tax_amount ? fmtAmt(d.tax_amount) : 'None') + '</div></div>'
    + '<div class="amount-box" style="border:1px solid rgba(16,185,129,0.2);background:rgba(16,185,129,0.05);"><div class="amount-box-label">Grand Total</div><div class="amount-box-val" style="color:var(--green-500);">' + fmtAmt(d.total_amount) + '</div></div>'
    + '</div>'

    // Line items
    + lineItemsHtml

    // JSON viewer
    + '<details style="margin-top:0.75rem;">'
    + '<summary style="font-size:12px;color:var(--text-500);cursor:pointer;user-select:none;padding:4px 0;">View raw JSON response</summary>'
    + '<div class="json-viewer" style="margin-top:0.75rem;">' + formatJson(d) + '</div>'
    + '</details>'
    + '</div>' // closes result-body

    + overlayHtml
    + '</div>' // closes result-body-container

    // Export bar
    + '<div class="export-bar">'
      + '<div style="display:flex;gap:6px;flex-wrap:wrap;">'
      + '<span class="badge badge-green">✓ pdfplumber</span>'
      + '<span class="badge badge-green">✓ Cerebras AI</span>'
      + (conf < 95 ? '<span class="badge badge-purple">+ Groq fallback</span>' : '<span class="badge badge-gold">High confidence</span>')
      + '</div>'
      + '<div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">'
      + '<button class="export-btn" onclick="copyJson()">📋 Copy JSON</button>'
      + (isLive
          ? '<button class="export-btn csv"    onclick="downloadCSV('   + exportId + ')">📊 CSV</button>'
          + '<button class="export-btn excel"  onclick="downloadExcel(' + exportId + ')">📗 Excel</button>'
          + '<button class="export-btn email-btn" onclick="toggleEmail()">✉️ Email</button>'
          : '<span style="font-size:11px;color:var(--text-600);">Upload a real PDF to enable CSV / Excel / Email</span>')
      + '</div>'
      + '</div>'

      // Email row (hidden until toggled)
      + '<div class="email-row" id="emailBar">'
      + '<input type="email" id="emailInput" placeholder="recipient@company.com" />'
      + '<button class="email-send-btn" onclick="sendEmail(' + exportId + ')">Send Excel</button>'
      + '<span class="email-status" id="emailStatus"></span>'
      + '</div>'

    + '</div>';   // closes result-card

  window._currentJson = d;
}

function fieldBox(label, value, extraClass) {
  var cls = 'field-box-val' + (extraClass ? ' ' + extraClass : '') + (!value ? ' empty-val' : '');
  return '<div class="field-box">'
    + '<div class="field-box-key">' + label + '</div>'
    + '<div class="' + cls + '">' + (value || 'not found') + '</div>'
    + '</div>';
}

// ── Export functions ─────────────────────────────────────────────────────────
function downloadFile(url, filename) {
  showToast('Preparing download...', 'success');
  
  fetch(url, { credentials: 'include' })
    .then(function(r) {
      if (r.status === 401) {
        showToast('Please log in to download this document.', 'error');
        throw new Error('Unauthorized');
      }
      if (!r.ok) throw new Error('Server returned ' + r.status);
      return r.blob();
    })
    .then(function(blob) {
      var objectUrl = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = objectUrl;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(function() { URL.revokeObjectURL(objectUrl); }, 5000);
      showToast(filename + ' downloaded', 'success');
    })
    .catch(function(err) {
      if (err.message !== 'Unauthorized') {
        showToast('Download failed — ' + err.message, 'error');
      }
    });
}

function downloadCSV(id) {
  downloadFile(API_BASE + '/documents/' + id + '/export/csv', 'invoice_' + id + '.csv');
}

function downloadExcel(id) {
  downloadFile(API_BASE + '/documents/' + id + '/export/excel', 'invoice_' + id + '.xlsx');
}

function toggleEmail() {
  var bar = document.getElementById('emailBar');
  if (bar) bar.classList.toggle('open');
}

function sendEmail(id) {
  var to = (document.getElementById('emailInput') || {}).value || '';
  to = to.trim();
  var status = document.getElementById('emailStatus');
  if (!to || !to.includes('@')) { showToast('Enter a valid email address', 'error'); return; }
  if (status) status.textContent = 'Sending...';

  fetch(API_BASE + '/documents/' + id + '/export/email?to=' + encodeURIComponent(to), {
    credentials: 'include'
  })
    .then(function(r) { 
      if (r.status === 401) {
        showToast('Please log in to export this document.', 'error');
        if (status) status.textContent = '✗ Unauthorized';
        throw new Error('Unauthorized');
      }
      return r.json(); 
    })
    .then(function(data) {
      if (data.success) {
        showToast('Email sent to ' + to, 'success');
        if (status) status.textContent = '✓ Sent to ' + to;
        var bar = document.getElementById('emailBar');
        if (bar) bar.classList.remove('open');
      } else {
        showToast(data.detail || 'Email failed', 'error');
        if (status) status.textContent = '✗ Failed';
      }
    })
    .catch(function(err) { 
      if (err.message !== 'Unauthorized') {
        showToast('Network error', 'error'); 
        if (status) status.textContent = '✗ Error';
      }
    });
}

// ── JSON helpers ─────────────────────────────────────────────────────────────
function formatJson(obj) {
  var str = JSON.stringify(obj, null, 2);
  return str
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"([^"]+)":/g, '<span class="json-key">"$1"</span>:')
    .replace(/: "([^"]*)"(,?\n)/g, ': <span class="json-str">"$1"</span>$2')
    .replace(/: (\d+\.?\d*)(,?\n)/g, ': <span class="json-num">$1</span>$2');
}

// ── Copy JSON ──
function copyJson() {
  if (!window._currentJson) return;
  navigator.clipboard.writeText(JSON.stringify(window._currentJson, null, 2))
    .then(function() { showToast('JSON copied to clipboard', 'success'); })
    .catch(function() { showToast('Copy failed — use Ctrl+C', 'error'); });
}

// ── API status check ─────────────────────────────────────────────────────────
fetch('https://doc-intelligence-api-tubh.onrender.com/health', { credentials: 'include' })
  .then(function(r) {
    var dot  = document.querySelector('.js-status-dot');
    var span = document.querySelector('.js-api-status');
    if (r.ok) {
      if (dot)  dot.style.background  = 'var(--green-500)';
      if (span) span.textContent = 'API Live';
    } else {
      if (span) span.textContent = 'API error';
    }
  })
  .catch(function() {
    var span = document.querySelector('.js-api-status');
    if (span) span.textContent = 'API offline (showing demo)';
  });

setTimeout(function() {
  const statusEl = document.querySelector('.js-api-status');
  if (statusEl && (statusEl.textContent === 'Checking API...' || statusEl.textContent === 'Connecting...')) {
    statusEl.textContent = 'API warming up (30s)...';
  }
}, 5000);
