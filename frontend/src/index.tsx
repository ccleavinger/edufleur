import { render } from 'solid-js/web';
import { createSignal, createMemo, createEffect, onMount, onCleanup, For, Show, Switch, Match } from 'solid-js';
import { api, states, type Image, type Catalog, type Course, type Unit } from './api';
import './style.css';

function Photo(props: { image: Image; alt: string }) {
  const [reason, setReason] = createSignal('wrongPlant'), [details, setDetails] = createSignal('');
  const [status, setStatus] = createSignal(''), [busy, setBusy] = createSignal(false), [failed, setFailed] = createSignal(false);
  let generation = 0;
  createEffect(() => { props.image.id; generation++; setStatus(''); setDetails(''); setReason('wrongPlant'); setFailed(false); setBusy(false); });

  async function report(event: SubmitEvent) {
    event.preventDefault(); setBusy(true); setStatus(''); const current = generation;
    try { await api('/images/' + props.image.id + '/reports', { reason: reason(), details: details() }); if (current === generation) setStatus('Report received. Thank you for helping improve this photo set.'); }
    catch (e) { if (current === generation) setStatus(String(e)); }
    finally { if (current === generation) setBusy(false); }
  }

  return <figure class="plant-photo">
    <Show when={!failed()} fallback={<div class="image-error" role="status">This photo could not load. Return to the course and try again.</div>}>
      <img src={props.image.url} alt={props.alt} loading="lazy" onError={() => setFailed(true)} />
    </Show>
    <figcaption><a href={props.image.sourceUrl} target="_blank" rel="noreferrer">{props.image.attribution}</a> · <a href={props.image.licenseUrl} target="_blank" rel="noreferrer">{props.image.license.toUpperCase()}</a></figcaption>
    <details class="report"><summary>Report photo</summary><form onSubmit={report}>
      <label>Reason<select value={reason()} onChange={e => setReason(e.currentTarget.value)}><option value="wrongPlant">Wrong plant</option><option value="unhelpful">Hard to identify</option><option value="license">License or attribution concern</option><option value="other">Something else</option></select></label>
      <label>Details (optional)<textarea maxlength={500} value={details()} onInput={e => setDetails(e.currentTarget.value)} /></label>
      <button disabled={busy()} type="submit">{busy() ? 'Sending…' : 'Send report'}</button><p role="status">{status()}</p>
    </form></details>
  </figure>;
}
function App() {
  const [url, setUrl] = createSignal(location.pathname + location.search);
  const pathname = () => url().split('?')[0];
  const region = () => { const code = new URLSearchParams(url().split('?')[1]).get('state')?.toUpperCase() || ''; return states[code] ? code : ''; };
  const habitatLink = () => '/habitats' + (region() && states[region()] ? '?state=' + region() : '');
  const [courses, setCourses] = createSignal<Course[]>([]), [catalog, setCatalog] = createSignal<Catalog>();
  const [progress, setProgress] = createSignal<Record<string, number>>({}), [error, setError] = createSignal('');
  const [course, setCourse] = createSignal<Course>(), [loading, setLoading] = createSignal(false);
  const [unitIndex, setUnitIndex] = createSignal<number>(), [phase, setPhase] = createSignal<'learn' | 'quiz' | 'complete'>('learn');
  const [questionIndex, setQuestionIndex] = createSignal(0), [choice, setChoice] = createSignal<number>();
  const [result, setResult] = createSignal<{ correct: boolean; explanation: string; correctName: string }>();
  const [busy, setBusy] = createSignal(false);
  let main: HTMLElement | undefined;
  let lessonGeneration = 0;
  function navigate(href: string) {
    history.pushState(null, '', href); setUrl(location.pathname + location.search); window.scrollTo(0, 0); main?.focus();
  }
  function links(event: MouseEvent) {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const a = (event.target as Element).closest('a[data-nav]') as HTMLAnchorElement | null;
    if (a) { event.preventDefault(); navigate(a.pathname + a.search); }
  }
  async function refreshProgress() { setProgress(await api('/progress')); }
  async function load() {
    setError('');
    try { const [c, meta] = await Promise.all([api<Course[]>('/courses'), api<Catalog>('/catalog')]); setCourses(c); setCatalog(meta); await refreshProgress(); }
    catch (e) { setError(String(e)); }
  }
  onMount(() => { void load(); const pop = () => { setUrl(location.pathname + location.search); main?.focus(); }; window.addEventListener('popstate', pop); onCleanup(() => window.removeEventListener('popstate', pop)); });
  createEffect(() => {
    const path = pathname();
    lessonGeneration++; setBusy(false);
    setCourse(undefined); setUnitIndex(undefined); setResult(undefined); setChoice(undefined); setError('');
    if (!path.startsWith('/courses/')) { setLoading(false); return; }
    const controller = new AbortController(); setLoading(true);
    api<Course>('/courses/' + encodeURIComponent(path.slice('/courses/'.length)), undefined, controller.signal)
      .then(c => { if (!controller.signal.aborted) setCourse(c); })
      .catch(e => { if (!controller.signal.aborted) setError(String(e)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    onCleanup(() => controller.abort());
  });
  const filtered = createMemo(() => courses().filter(c => !region() || c.stateCode === region()));
  const unit = (): Unit | undefined => unitIndex() === undefined ? undefined : course()?.units[unitIndex()!];
  const question = () => unit()?.questions[questionIndex()];
  function startUnit(index: number) { lessonGeneration++; setBusy(false); setUnitIndex(index); setPhase('learn'); setQuestionIndex(0); setChoice(undefined); setResult(undefined); setError(''); window.scrollTo(0, 0); }
  async function check() {
    const c = course(), q = question(), selected = choice(); if (!c || !q || selected === undefined || busy()) return;
    const generation = lessonGeneration; setBusy(true); setError('');
    try {
      const answer = await api<{ correct: boolean; explanation: string; correctName: string }>('/courses/' + c.id + '/answers', { question: q.id, choice: selected, catalogVersion: c.catalogVersion });
      // Navigation during an in-flight request must not alter a different lesson.
      if (generation !== lessonGeneration || course()?.id !== c.id || question()?.id !== q.id) return;
      setResult(answer);
      try { await refreshProgress(); } catch { setError('Your answer was saved, but progress could not refresh. Reload to see the latest count.'); }
    } catch (e) { if (generation === lessonGeneration && course()?.id === c.id) setError(String(e)); }
    finally { if (generation === lessonGeneration) setBusy(false); }
  }
  function next() {
    if (!result()?.correct) { setResult(undefined); setChoice(undefined); return; }
    if (questionIndex() + 1 === unit()!.questions.length) setPhase('complete');
    else { setQuestionIndex(questionIndex() + 1); setChoice(undefined); setResult(undefined); }
  }
  const learned = () => Object.values(progress()).reduce((a, b) => a + b, 0);
  return <div onClick={links}>
    <header><a data-nav href="/" class="brand"><span class="brand-icon">✳</span> edufleur</a><nav aria-label="Main navigation"><a data-nav href="/" aria-current={pathname() === '/' ? 'page' : undefined}>Home</a><a data-nav href={habitatLink()} aria-current={pathname() === '/habitats' ? 'page' : undefined}>Habitats</a></nav><span class="header-note">A little closer to nature.</span></header>
    <main ref={main} tabindex="-1">
      <Show when={error()}><div role="alert" class="error">{error()} <button onClick={() => location.reload()}>Reload</button></div></Show>
      <Switch fallback={<section class="page-intro"><h1>Off the trail.</h1><p>This page could not be found.</p><a data-nav class="button" href="/habitats">Explore habitats →</a></section>}>
        <Match when={pathname() === '/'}>
          <section class="hero"><div><span class="eyebrow">YOUR NEXT DISCOVERY IS OUTSIDE</span><h1>Learn your<br />landscape.</h1><p>Get to know the native plants around you.<br />One ecological area. One small discovery at a time.</p><a data-nav class="button" href="/habitats">Find your habitat <span>↗</span></a></div><div class="botanical" aria-hidden="true"><svg viewBox="0 0 360 360"><circle cx="180" cy="180" r="152" fill="#dae4cb" /><path d="M181 319 Q163 200 211 65 M179 246 Q108 234 90 156 M186 204 Q264 182 275 116" fill="none" stroke="#47674d" stroke-width="5" /><g fill="#67865a"><ellipse cx="115" cy="194" rx="22" ry="55" transform="rotate(-40 115 194)" /><ellipse cx="166" cy="228" rx="20" ry="46" transform="rotate(20 166 228)" /><ellipse cx="244" cy="155" rx="21" ry="55" transform="rotate(40 244 155)" /><ellipse cx="201" cy="116" rx="22" ry="53" transform="rotate(15 201 116)" /></g><g fill="#e4b75a"><circle cx="211" cy="66" r="17" /><circle cx="193" cy="60" r="13" /><circle cx="223" cy="51" r="14" /><circle cx="211" cy="46" r="12" /></g></svg><span class="field-note">NOTICE MORE. KNOW MORE.</span></div></section>
          <section class="how-it-works" aria-label="How learning works"><article><span class="eyebrow">01 / FIND YOUR PLACE</span><h3>Start where you are.</h3><p>Choose a state and ecological area. Explore several places, or find a new course when you move.</p></article><article><span class="eyebrow">02 / LOOK A LITTLE CLOSER</span><h3>Learn through real photos.</h3><p>Meet a small group of plants, then practice recognizing them in a different photograph.</p></article><article><span class="eyebrow">03 / KEEP DISCOVERING</span><h3>Build familiarity.</h3><p>Return to your courses and practice again. Your progress stays with this browser.</p></article></section>
        </Match>
        <Match when={pathname() === '/habitats'}>
          <section class="page-intro"><span class="eyebrow">PUT DOWN ROOTS</span><h1>Find your habitat.</h1><p>Explore ecological areas across the lower 48. Each course brings a different landscape into focus.</p></section>
          <section class="stats"><div><strong>{learned()}</strong><span>plants practiced</span></div><div><strong>{Object.keys(progress()).length}</strong><span>areas explored</span></div><div><strong>{courses().filter(c => c.available).length}</strong><span>courses ready</span></div></section>
          <div class="section-heading"><h2>Explore an area</h2><label>Choose a state<select value={region()} onChange={e => navigate('/habitats' + (e.currentTarget.value ? '?state=' + e.currentTarget.value : ''))}><option value="">All states</option><For each={Object.entries(states)}>{([code, name]) => <option value={code}>{name}</option>}</For></select></label></div>
          <p class="muted">Courses follow EPA Level III ecoregions. Only areas with enough native-status evidence and licensed photographs are playable.</p>
          <Show when={catalog()} fallback={<p role="status">Loading the catalog…</p>}>
            <Show when={catalog()?.ready} fallback={<aside class="note"><span>✳</span><div><strong>The field guide is still growing.</strong><p>A dataset-backed catalog has not been published yet. The sample courses have been retired; your earlier progress is retained.</p></div></aside>}>
              <Show when={filtered().length} fallback={<aside class="note"><p>No ecological areas have been imported for {states[region()] || 'this selection'} yet.</p></aside>}>
                <div class="cards"><For each={filtered()}>{(c, i) => <article class={'course theme-' + (i() % 2)}><div class="course-art" aria-hidden="true">✳<span>{c.state.toUpperCase()} / ECOREGION {c.ecoregionCode}</span></div><div class="course-body"><span class="eyebrow">{c.state}</span><h3>{c.habitat}</h3><p>{c.available ? c.description : c.reason}</p><Show when={c.available}><div class="meter"><div style={{ width: `${(progress()[c.id] || 0) / c.total * 100}%` }} /></div></Show><div class="course-bottom"><small>{c.available ? `${progress()[c.id] || 0} / ${c.total} plants practiced` : 'More source data needed'}</small><a data-nav href={'/courses/' + c.id + '?state=' + c.stateCode}>{c.available ? 'Explore course →' : 'View availability →'}</a></div></div></article>}</For></div>
              </Show>
            </Show>
          </Show>
        </Match>
        <Match when={pathname().startsWith('/courses/')}>
          <a data-nav class="back" href={habitatLink()}>← All habitats</a>
          <Show when={!loading()} fallback={<p role="status">Opening your field notes…</p>}>
            <Show when={course()}>{c => <>
              <Show when={unit()} fallback={<section class="page-intro"><span class="eyebrow">{c().state} / ECOREGION {c().ecoregionCode}</span><h1>{c().habitat}</h1><p>{c().description}</p><Show when={c().available} fallback={<aside class="note"><p>{c().reason}</p></aside>}><p>{progress()[c().id] || 0} of {c().total} plants practiced. Work through a unit or return to one you know.</p><div class="units"><For each={c().units}>{(u, i) => <button onClick={() => startUnit(i())}><span class="eyebrow">UNIT {i() + 1}</span><strong>{u.title}</strong><span>{u.plants.length} plants →</span></button>}</For></div></Show></section>}>
                <section class="lesson generated-lesson"><button class="back" disabled={busy()} onClick={() => { setUnitIndex(undefined); setError(''); }}>← Course overview</button><span class="eyebrow">{c().habitat} / {unit()?.title}</span>
                  <Switch>
                    <Match when={phase() === 'learn'}><h2>Meet your neighbors.</h2><p>Take a moment with each plant. You’ll see a different photograph when it’s time to practice.</p><div class="intro-plants"><For each={unit()?.plants}>{p => <article><h3>{p.name}</h3><i>{p.scientificName}</i><Photo image={p.image} alt={p.name + ' introduction photograph'} /></article>}</For></div><button class="button" onClick={() => { setPhase('quiz'); window.scrollTo(0, 0); }}>Ready to practice →</button></Match>
                    <Match when={phase() === 'quiz'}><div class="lesson-top"><h2>Look a little closer.</h2><span>{questionIndex() + 1} / {unit()?.questions.length}</span></div><progress aria-label="Unit progress" value={questionIndex()} max={unit()?.questions.length} /><Show when={question()} keyed>{q => <><h3>{q.prompt}</h3><Photo image={q.image} alt="Plant to identify" /><div class="answers"><For each={q.options}>{(option, i) => <button class={choice() === i() ? 'selected' : ''} disabled={busy() || !!result()} aria-pressed={choice() === i()} onClick={() => setChoice(i())}>{option}</button>}</For></div></>}</Show><Show when={result()}><div role="status" class={'feedback ' + (result()?.correct ? 'correct' : '')}><strong>{result()?.correct ? 'You spotted it.' : 'Take another look.'}</strong><p>{result()?.explanation}</p></div></Show><button class="button" disabled={busy() || choice() === undefined} onClick={() => result() ? next() : check()}>{busy() ? 'Saving…' : result()?.correct ? 'Continue →' : result() ? 'Try again' : 'Check answer'}</button></Match>
                    <Match when={phase() === 'complete'}><h2>A little more familiar.</h2><p>You’ve finished this unit. Keep exploring or revisit the plants to strengthen your recognition.</p><div class="actions"><Show when={unitIndex()! + 1 < c().units.length}><button class="button" onClick={() => startUnit(unitIndex()! + 1)}>Next unit →</button></Show><button onClick={() => startUnit(unitIndex()!)}>Practice again</button><button onClick={() => setUnitIndex(undefined)}>Course overview</button></div></Match>
                  </Switch>
                </section>
              </Show>
            </>}</Show>
          </Show>
        </Match>
      </Switch>
    </main>
    <footer><span>edufleur · Make yourself at home in nature.</span><span>Lower 48 · Progress belongs to this browser</span></footer>
  </div>;
}
render(() => <App />, document.getElementById('root')!);
