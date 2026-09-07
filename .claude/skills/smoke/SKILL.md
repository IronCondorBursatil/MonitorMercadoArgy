---
name: smoke
description: Levanta la app en 127.0.0.1:8001 (sin tocar el :8000), verifica /api/health, /login y UNA ruta nueva o tocada por el cambio, y limpia el puerto pase lo que pase. Protocolo del puerto 8001 (matar listeners stale antes de arrancar) incluido.
argument-hint: "[ruta]"
shell: powershell
allowed-tools: PowerShell(Get-NetTCPConnection *), PowerShell(Stop-Process *), PowerShell(Start-Process *), PowerShell(Get-Process *), PowerShell(Invoke-RestMethod *), PowerShell(Invoke-WebRequest *)
---

# /smoke [ruta] — la app levanta, y la ruta que cambiaste está viva

`scripts/run_server_test.py` levanta `apps/web/app.py` en `127.0.0.1:8001` con uvicorn, sin
tocar el `:8000` productivo. El smoke prueba tres cosas, en orden: que arranca y carga el
catálogo (`/api/health`), que la auth está viva (`/login`), y que **la ruta del cambio**
responde desde el código actual.

**Ruta obligatoria.** Si no vino como argumento, pedirla antes de arrancar: un panel que viaja
por otro endpoint NO prueba que la ruta nueva exista (incidente real: `/api/snapshot` daba
data nueva y una ruta nueva daba 404, porque respondían DOS procesos distintos).

## Por qué el puerto se barre antes de arrancar

En Windows, con `SO_REUSEADDR`, pueden quedar **varios procesos co-bindeados** al 8001 y las
conexiones caen en uno viejo con código previo. Costó 4 pasos de diagnóstico. Regla: ANTES
de arrancar, matar TODOS los listeners del 8001 hasta que el puerto quede libre; recién ahí
`Start-Process`.

Detalles que importan:

- Se arranca **`python.exe` directo**, no `py -3.12`: el launcher `py` es un proceso padre
  que espera al `python.exe` hijo; `Stop-Process` sobre el PID del launcher deja al hijo vivo
  con el puerto tomado — exactamente el problema que se quiere evitar.
- **Las variables no sobreviven entre llamadas a la tool** (cada `PowerShell` es un proceso
  nuevo): por eso el protocolo va en UN solo script con `try/finally`, que guarda el PID en
  `$srv` y lo mata aunque falle un check.
- `Stop-Process` está en `ask` en `.claude/settings.json` y el `ask` gana sobre cualquier
  allow: si pregunta, es esperado.

## El script (una sola llamada; timeout de la tool 180000 ms)

Correr desde la raíz del repo. Reemplazar `<ruta>` y `$expect` (ver tabla de abajo). Los
logs del server quedan en `$log.out.txt` / `$log.err.txt` para leerlos con `Read` si algo
falla.

```powershell
$ruta   = "/<ruta>"          # la ruta nueva o tocada por el cambio
$expect = 302                # 302 = protegida sin cookie · 200 = pública
$port   = 8001
$base   = "http://127.0.0.1:$port"
$py     = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
$log    = Join-Path $env:TEMP "smoke-$port"     # o el scratchpad de la sesión
$srv    = $null
$ok     = $true

function Get-Listeners { Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue }
function Clear-Port {
    for ($i = 0; $i -lt 5; $i++) {
        $l = Get-Listeners
        if (-not $l) { return $true }
        $l | Select-Object -ExpandProperty OwningProcess -Unique |
            ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 1
    }
    return (-not (Get-Listeners))
}

try {
    # 1. Puerto libre ANTES de arrancar (loop hasta que no quede ningún listener)
    if (-not (Clear-Port)) { throw "8001 sigue ocupado tras 5 intentos: $((Get-Listeners).OwningProcess -join ',')" }

    # 2. Server en background, PID guardado
    $srv = Start-Process -FilePath $py -ArgumentList "scripts/run_server_test.py" `
        -WorkingDirectory $PWD -PassThru -NoNewWindow `
        -RedirectStandardOutput "$log.out.txt" -RedirectStandardError "$log.err.txt"
    "server PID $($srv.Id)"

    # 3. /api/health (público) — hasta ~60 s: el arranque carga el catálogo
    $h = $null
    for ($i = 0; $i -lt 30; $i++) {
        if ($srv.HasExited) { throw "el server murió al arrancar (exit $($srv.ExitCode)); leer $log.err.txt" }
        try { $h = Invoke-RestMethod "$base/api/health" -TimeoutSec 5; break } catch { Start-Sleep -Seconds 2 }
    }
    if (-not $h) { throw "sin /api/health en 60 s; leer $log.err.txt" }
    "health: status=$($h.status) instruments=$($h.instruments) is_stale=$($h.is_stale) age=$($h.age_seconds) degraded=$($h.degraded_loops -join ',')"
    if ($h.instruments -le 0) { $ok = $false; Write-Warning "instruments=0: el catálogo no cargó" }

    # 3b. status pasa de 'degraded' a 'ok' cuando termina el PRIMER ciclo de refresh (~5-15 s)
    for ($i = 0; $i -lt 8 -and $h.status -ne "ok"; $i++) { Start-Sleep -Seconds 3; $h = Invoke-RestMethod "$base/api/health" -TimeoutSec 5 }
    "status final: $($h.status) (is_stale=$($h.is_stale), age=$($h.age_seconds))"

    # 4. /login → 200
    $r = Invoke-WebRequest "$base/login" -TimeoutSec 10
    "login: $($r.StatusCode)"
    if ($r.StatusCode -ne 200) { $ok = $false }

    # 5. La ruta del cambio, SIN cookie: 302→/login = auth viva; 200 si es pública; 404 = NO está en el código que corre
    #    (según la versión de PS, un 3xx con -MaximumRedirection 0 vuelve como respuesta o como excepción: se leen las dos)
    try   { $r = Invoke-WebRequest "$base$ruta" -MaximumRedirection 0 -SkipHttpErrorCheck -TimeoutSec 15
            $code = [int]$r.StatusCode; $loc = "$($r.Headers.Location)" }
    catch { $code = [int]$_.Exception.Response.StatusCode; $loc = "$($_.Exception.Response.Headers.Location)" }
    "ruta $ruta -> $code $loc"
    if ($code -ne $expect) { $ok = $false; Write-Warning "esperaba $expect y dio $code" }
}
catch { $ok = $false; Write-Warning $_ }
finally {
    # 6. Cleanup SIEMPRE: matar por PID y volver a barrer el puerto (por si quedó un hijo huérfano)
    if ($srv -and -not $srv.HasExited) { Stop-Process -Id $srv.Id -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 1
    if (Clear-Port) { "8001 libre" } else { Write-Warning "8001 SIGUE ocupado: $((Get-Listeners).OwningProcess -join ',')" }
    if ($ok) { "=== SMOKE OK ===" } else { "=== SMOKE FALLÓ ===" }
}
```

## Qué esperar de la ruta

| Ruta | Sin cookie | Significa |
|---|---|---|
| protegida (`/panels/...`, `/bond/...`, `/fci`, `/on`, `/abm`...) | **302** con `Location: /login...` | la ruta existe y la auth está viva |
| pública (`/api/health`, `/login`) | **200** | ok |
| cualquiera | **404** | la ruta NO está en el proceso que responde: código viejo, router no incluido en `app.py`, o typo |
| cualquiera | **500** | bug al importar/renderizar — leer `$log.err.txt` |
| cualquiera | **403** | logueado pero sin permiso de pestaña; con cookie ausente no debería aparecer |

`/api/health` devuelve `status` (`ok`/`degraded` = habla de los PRECIOS), `instruments`,
`is_stale`, `age_seconds`, `degraded_loops`, `loop_crashes_24h`, `catalog`. Recién
arrancado está `degraded` hasta que corre el primer refresh; si sigue así después de ~30 s,
es la red hacia los providers (BYMA/Data912) o un loop caído — mirar `$log.err.txt`. Sin
internet, lo duro es `instruments > 0`; el `status` se reporta como está.

## Qué NO hace

- No prueba con sesión (no hay credenciales en el skill). Para un flujo logueado, Playwright
  (skill `verificar-ui`, Fase 3).
- No reemplaza al gate (`/gate`) ni al health remoto de `/deploy`.
- Si el server tiene que quedar vivo para seguir probando (Playwright), persistir `$srv.Id`
  en un archivo del scratchpad y correr el bloque `finally` como llamada aparte al terminar:
  sin eso, el listener queda huérfano y el próximo smoke arranca con el puerto sucio.
