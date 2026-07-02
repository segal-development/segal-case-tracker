// ============================================================================
// Mis Causas civil → descarga JSON, corriendo en TU Chrome ya logueado.
// Pegá esto en la CONSOLA de Chrome (Cmd+Option+J) estando DENTRO de PJUD
// (Mis Causas / indexN.php). Usa el propio jQuery + tu sesión → pasa Shape.
// Al terminar descarga "carla_causas.json". Cambiá RUT/DV para otro abogado.
// ============================================================================
(async () => {
  const ENDPOINT = 'https://oficinajudicialvirtual.pjud.cl/misCausas/civil/consultaMisCausasCivil.php';
  const RUT = '16021492', DV = '9';        // Carla Lavín (16021492-9)
  const OUT = 'carla_causas.json';

  const post = (pagina) => new Promise((resolve) => {
    $.ajax({
      url: ENDPOINT, type: 'POST',
      data: {
        rutMisCauCiv: RUT, dvMisCauCiv: DV, tipoMisCauCiv: '0', rolMisCauCiv: '',
        anhoMisCauCiv: '', fecDesdeMisCauCiv: '', fecHastaMisCauCiv: '',
        'tipCausaMisCauCiv[]': 'M', nombreMisCauCiv: '', apePatMisCauCiv: '',
        apeMatMisCauCiv: '', pagina: pagina
      },
      success: (d) => resolve(d),
      error: (x) => resolve('ERROR:' + x.status)
    });
  });

  const first = await post(1);
  if (typeof first !== 'string' || first.startsWith('ERROR:')) {
    console.error('Falló la página 1 (¿estás logueado en Mis Causas?):', first); return;
  }
  const fin = first.match(/onclick="pagina\((\d+),\d+\);">Fin/i);
  const tot = first.match(/Total de registros:\s*<b>(\d+)<\/b>/i);
  const totalCount = tot ? parseInt(tot[1]) : null;
  let totalPages = fin ? parseInt(fin[1]) : 1;
  if (totalPages === 1 && totalCount && totalCount > 15) totalPages = Math.ceil(totalCount / 15);
  console.log(`Total causas: ${totalCount} · páginas: ${totalPages}`);

  const pages = [first];
  for (let p = 2; p <= totalPages; p++) {
    const html = await post(p);
    pages.push(html);
    console.log(`página ${p}/${totalPages} · len ${(html || '').length}`);
    await new Promise(r => setTimeout(r, 350));   // espaciado suave
  }

  const blob = new Blob([JSON.stringify(pages)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = window.URL.createObjectURL(blob);
  a.download = OUT;
  document.body.appendChild(a); a.click(); a.remove();
  console.log(`✅ LISTO — ${pages.length} páginas descargadas en ${OUT}`);
})();
