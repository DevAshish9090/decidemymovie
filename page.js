/* =========================================================
   DecideMyMovie — shared chrome for the static sub-pages.
   Injects the header and footer so there is ONE place to edit
   them, instead of the same markup copy-pasted into 7 files.

   Each page sets <body data-page="privacy"> so the matching
   header link can be highlighted.
   ========================================================= */
(function () {
  var page = document.body.getAttribute('data-page') || '';

  var NAV = [
    { href: 'index.html',        label: 'Home',         key: 'home' },
    { href: 'about.html',        label: 'About',        key: 'about' },
    { href: 'how-it-works.html', label: 'How it works', key: 'how' },
    { href: 'contact.html',      label: 'Contact',      key: 'contact' }
  ];

  var ICONS = {
    x: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24h-6.66l-5.214-6.817-5.97 6.817H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231 5.45-6.231zm-1.161 17.52h1.833L7.084 4.126H5.117l11.966 15.644z"/></svg>',
    ig: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="2" width="20" height="20" rx="5.5"/><circle cx="12" cy="12" r="4"/><circle cx="17.6" cy="6.4" r="1.1" fill="currentColor" stroke="none"/></svg>',
    yt: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M23 12s0-3.5-.44-5.17a2.78 2.78 0 0 0-1.95-1.96C18.94 4.43 12 4.43 12 4.43s-6.94 0-8.61.44a2.78 2.78 0 0 0-1.95 1.96C1 8.5 1 12 1 12s0 3.5.44 5.17a2.78 2.78 0 0 0 1.95 1.96c1.67.44 8.61.44 8.61.44s6.94 0 8.61-.44a2.78 2.78 0 0 0 1.95-1.96C23 15.5 23 12 23 12zM9.75 15.02V8.98L15.5 12z"/></svg>',
    li: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M20.45 20.45h-3.56v-5.57c0-1.33-.03-3.04-1.85-3.04-1.85 0-2.14 1.45-2.14 2.94v5.67H9.35V9h3.41v1.56h.05c.48-.9 1.63-1.85 3.36-1.85 3.6 0 4.27 2.37 4.27 5.45v6.29zM5.34 7.43a2.06 2.06 0 1 1 0-4.13 2.06 2.06 0 0 1 0 4.13zm1.78 13.02H3.55V9h3.57v11.45zM22.22 0H1.77C.79 0 0 .77 0 1.72v20.56C0 23.23.79 24 1.77 24h20.45c.98 0 1.78-.77 1.78-1.72V1.72C24 .77 23.2 0 22.22 0z"/></svg>',
    gh: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 .5C5.37.5 0 5.87 0 12.5c0 5.3 3.44 9.8 8.21 11.39.6.11.82-.26.82-.58 0-.29-.01-1.04-.02-2.05-3.34.73-4.04-1.61-4.04-1.61-.55-1.39-1.34-1.76-1.34-1.76-1.09-.75.08-.73.08-.73 1.2.09 1.84 1.24 1.84 1.24 1.07 1.83 2.81 1.3 3.5.99.11-.78.42-1.3.76-1.6-2.67-.3-5.47-1.33-5.47-5.93 0-1.31.47-2.38 1.24-3.22-.13-.3-.54-1.52.11-3.18 0 0 1.01-.32 3.3 1.23a11.5 11.5 0 0 1 6 0c2.29-1.55 3.3-1.23 3.3-1.23.65 1.66.24 2.88.12 3.18.77.84 1.23 1.91 1.23 3.22 0 4.61-2.81 5.63-5.49 5.92.43.37.82 1.1.82 2.22 0 1.6-.02 2.9-.02 3.29 0 .32.22.7.83.58A12.01 12.01 0 0 0 24 12.5C24 5.87 18.63.5 12 .5z"/></svg>',
    mail: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/></svg>'
  };

  function header() {
    var links = NAV.map(function (n) {
      return '<a href="' + n.href + '"' + (n.key === page ? ' class="is-here"' : '') + '>' + n.label + '</a>';
    }).join('');
    return '<header class="phead">'
      + '<a href="index.html" aria-label="DecideMyMovie home"><img src="logo.png" class="phead__logo" alt="DecideMyMovie"></a>'
      + '<nav class="phead__nav">' + links + '</nav>'
      + '</header>';
  }

  function footer() {
    var year = new Date().getFullYear();
    return '<footer class="foot">'
      + '<div class="foot__top">'
      +   '<div class="foot__brandcol">'
      +     '<div class="foot__brand"><a href="index.html"><img src="logo.png" class="foot__logo" alt="DecideMyMovie"></a></div>'
      +     '<p class="foot__blurb">Stop scrolling, start watching. Tell us the mood, describe a half-remembered scene, or let fate decide, and we&rsquo;ll point you to the one film worth your night.</p>'
      +     '<div class="foot__socials">'
      +       '<a class="foot__soc" href="https://www.linkedin.com/in/devashish9090/" target="_blank" rel="noopener noreferrer" aria-label="LinkedIn">' + ICONS.li + '</a>'
      +       '<a class="foot__soc" href="https://github.com/DevAshish9090" target="_blank" rel="noopener noreferrer" aria-label="GitHub">' + ICONS.gh + '</a>'
      +     '</div>'
      +   '</div>'
      +   '<div class="foot__col"><h4>Explore</h4><ul>'
      +     '<li><a href="index.html#browse">Trending</a></li>'
      +     '<li><a href="index.html#browse">Categories</a></li>'
      +     '<li><a href="index.html#explore">Moods</a></li>'
      +     '<li><a href="index.html#mylist">My Watchlist</a></li>'
      +   '</ul></div>'
      +   '<div class="foot__col"><h4>Company</h4><ul>'
      +     '<li><a href="about.html">About</a></li>'
      +     '<li><a href="how-it-works.html">How it works</a></li>'
      +     '<li><a href="blog.html">Blog</a></li>'
      +     '<li><a href="contact.html">Contact</a></li>'
      +   '</ul></div>'
      +   '<div class="foot__news"><h4>Stay in the loop</h4>'
      +     '<p>New picks, hidden gems and features, straight to your inbox. No spam, ever.</p>'
      +     '<div class="foot__form" id="subForm">'
      +       '<input class="foot__input" type="email" placeholder="you@email.com" aria-label="Email address">'
      +       '<button class="foot__send" type="button" id="subBtn">Subscribe</button>'
      +     '</div>'
      +     '<span class="foot__mail">' + ICONS.mail + '<a href="mailto:contact@decidemymovie.com">contact@decidemymovie.com</a></span>'
      +   '</div>'
      + '</div>'
      + '<div class="foot__bar"><div class="foot__barin">'
      +   '<div class="foot__barrow">'
      +     '<span class="foot__copy">&copy; ' + year + ' DecideMyMovie. All rights reserved.</span>'
      +     '<div class="foot__legal">'
      +       '<a href="privacy.html">Privacy</a><a href="terms.html">Terms</a><a href="cookies.html">Cookies</a>'
      +     '</div>'
      +   '</div>'
      +   '<p class="foot__fine"><img src="tmdb-logo.svg" class="foot__tmdb" alt="TMDB" loading="lazy" onerror="this.style.display=\'none\'">Movie data provided by TMDB. This product uses the TMDB API but is not endorsed or certified by TMDB. All film titles, posters and streaming logos are the property of their respective owners; DecideMyMovie is not affiliated with, sponsored by, or endorsed by any streaming service.</p>'
      + '</div></div>'
      + '</footer>';
  }

  var h = document.getElementById('site-header');
  if (h) h.outerHTML = header();
  var f = document.getElementById('site-footer');
  if (f) f.outerHTML = footer();

  // newsletter subscribe (mirrors the home page footer)
  (function(){
    var form = document.getElementById('subForm');
    var btn  = document.getElementById('subBtn');
    if (!form || !btn) return;
    var API = (window.DMM_API || 'http://localhost:8000');
    var input = form.querySelector('.foot__input');
    var re = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
    function done(msg, ok){
      form.innerHTML = '<span style="color:'+(ok?'var(--gold)':'#e08a7d')+';font-size:.92rem;font-weight:600;">'+msg+'</span>';
    }
    input.addEventListener('input', function(){ input.style.borderColor=''; });
    btn.addEventListener('click', async function(){
      var val = (input.value||'').trim();
      if (!re.test(val)) { input.focus(); input.style.borderColor='#c8503f'; return; }
      btn.disabled = true; var label = btn.textContent; btn.textContent = '…';
      try{
        var r = await fetch(API + '/api/subscribe', { method:'POST',
          headers:{'Content-Type':'application/json'},
          body: JSON.stringify({ email: val, source: 'footer' }) });
        var d = await r.json().catch(function(){ return {}; });
        if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
        done('Thanks, you&rsquo;re on the list.', true);
      }catch(e){
        btn.disabled = false; btn.textContent = label;
        input.style.borderColor = '#c8503f';
        done('Couldn&rsquo;t sign you up just now. Please try again later.', false);
      }
    });
    input.addEventListener('keydown', function(e){ if (e.key === 'Enter'){ e.preventDefault(); btn.click(); } });
  })();
})();
