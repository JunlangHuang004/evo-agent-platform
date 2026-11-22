(function(){
  var slides = Array.prototype.slice.call(document.querySelectorAll('.slide'));
  var bar = document.getElementById('progress');
  var counter = document.getElementById('counter');
  var i = 0;
  function show(n){
    if(n<0) n=0;
    if(n>slides.length-1) n=slides.length-1;
    slides[i].classList.remove('active');
    i=n;
    slides[i].classList.add('active');
    if(bar) bar.style.width = ((i+1)/slides.length*100)+'%';
    if(counter) counter.textContent = (i+1)+' / '+slides.length;
  }
  document.addEventListener('keydown', function(e){
    if(e.key==='ArrowRight'||e.key==='PageDown'||e.key===' ') { e.preventDefault(); show(i+1); }
    else if(e.key==='ArrowLeft'||e.key==='PageUp') { e.preventDefault(); show(i-1); }
    else if(e.key==='Home'){ show(0); }
    else if(e.key==='End'){ show(slides.length-1); }
  });
  var prev=document.getElementById('prev'), next=document.getElementById('next');
  if(prev) prev.addEventListener('click', function(){ show(i-1); });
  if(next) next.addEventListener('click', function(){ show(i+1); });
  show(0);
})();
