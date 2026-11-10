/* 图片查看器 V1.5.3
   用途：点缩略图 → 全屏查看。默认"适应屏幕"（不放大、不撑满），
   点一下图切"原始大小"，可滚动/拖动；另有「查看原图」按钮按需加载原图。
   为什么这么改：安卓/微信 WebView 点 <a target="_blank"> 会走浏览器原生
   大图页，尺寸不可控（用户看到的就是"一开就放到最大"）。 */
(function () {
  if (window.__imageViewerInstalled) return;
  window.__imageViewerInstalled = true;

  var VIEW_W = 1280;   // 默认显示的压缩图宽度（原图 11MB，手机上直接看会卡）

  // ---- 注入 DOM ----
  var overlay = document.createElement('div');
  overlay.id = 'imgviewer-overlay';
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-modal', 'true');
  overlay.innerHTML =
    '<button id="imgviewer-close" type="button" aria-label="关闭">&times;</button>' +
    '<button id="imgviewer-original" type="button">查看原图</button>' +
    '<div id="imgviewer-stage"><img id="imgviewer-img" alt=""></div>' +
    '<div id="imgviewer-caption"></div>' +
    '<div id="imgviewer-hint">点一下图片可放大到原始大小，再点一下还原</div>';
  document.body.appendChild(overlay);

  var imgEl = overlay.querySelector('#imgviewer-img');
  var capEl = overlay.querySelector('#imgviewer-caption');
  var btnClose = overlay.querySelector('#imgviewer-close');
  var btnOriginal = overlay.querySelector('#imgviewer-original');
  var curFull = '';     // 原图地址
  var curSrc = '';      // 当前显示的地址

  // 把 /uploads/xxx.jpg 换成 /photo/xxx.jpg?w=1280（服务端压缩，秒开）
  function toViewUrl(url, w) {
    if (!url) return '';
    var i = url.indexOf('/uploads/');
    if (i < 0) return url;
    return url.slice(0, i) + '/photo/' + url.slice(i + '/uploads/'.length) + '?w=' + (w || VIEW_W);
  }

  function open(src, alt) {
    if (!src) return;
    curFull = src;
    curSrc = toViewUrl(src);
    imgEl.src = curSrc;
    imgEl.alt = alt || '';
    capEl.textContent = alt || '';
    overlay.classList.remove('zoom');
    overlay.classList.add('show');
    document.documentElement.style.overflow = 'hidden';
  }
  function close() {
    overlay.classList.remove('show', 'zoom');
    imgEl.src = '';
    document.documentElement.style.overflow = '';
  }
  function toggleZoom() {
    overlay.classList.toggle('zoom');
    // 切到原始大小时，如果没有加载过原图就先按屏幕宽度看
    if (overlay.classList.contains('zoom')) {
      overlay.querySelector('#imgviewer-stage').scrollTop = 0;
    }
  }

  // ---- 交互 ----
  btnClose.addEventListener('click', function (e) { e.stopPropagation(); close(); });
  btnOriginal.addEventListener('click', function (e) {
    e.stopPropagation();
    if (curFull && imgEl.src.indexOf('/uploads/') >= 0) return;  // 已经是原图
    imgEl.src = curFull;
    btnOriginal.textContent = '已加载原图';
  });
  imgEl.addEventListener('click', function (e) { e.stopPropagation(); toggleZoom(); });
  overlay.addEventListener('click', function (e) { if (e.target === overlay) close(); });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && overlay.classList.contains('show')) close();
  });

  // 事件委托：任何 .photo-thumb（图本身或外层容器）都接管，阻止原生跳转
  document.addEventListener('click', function (e) {
    var t = e.target;
    while (t && t !== document.body) {
      if (t.classList && t.classList.contains('photo-thumb')) {
        var src = (t.getAttribute && (t.getAttribute('data-full') || t.getAttribute('src'))) || '';
        if (!src && t.querySelector) {
          var inner = t.querySelector('img');
          src = inner ? (inner.getAttribute('data-full') || inner.getAttribute('src')) : '';
        }
        if (!src) { t = t.parentNode; continue; }
        e.preventDefault();
        e.stopPropagation();
        open(src, (t.getAttribute && t.getAttribute('alt')) || '');
        return;
      }
      t = t.parentNode;
    }
  }, true);   // 捕获阶段，优先于浏览器默认行为

  window.openPhotoViewer = open;
  window.closePhotoViewer = close;
})();
