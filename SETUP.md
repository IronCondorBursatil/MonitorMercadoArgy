# Setup — cómo correrlo en otra PC

**Requisito único:** tener **Python 3.12** instalado  
(https://www.python.org/downloads/ → marcá *"Add python.exe to PATH"* al instalar).

---

## Primera vez en una PC

1. Copiá la carpeta del proyecto a la PC nueva.
2. Doble-click en **`setup.bat`**.  
   Instala las dependencias exactas de `requirements.txt` con `py -3.12`.  
   (Tarda un rato la primera vez; necesita internet.)

## Para usarlo (cada vez)

- Doble-click en **`run.bat`** → arranca el dashboard en  
  **http://localhost:8000** (abrí esa URL en el navegador).
- Para frenarlo: `Ctrl+C` en la ventana negra.

También podés correr directamente desde la terminal:

```bat
py -3.12 run.py
```

---

## Mantenimiento

- Si cambiás dependencias en `requirements.txt`, volvé a correr **`setup.bat`**.

## Si algo falla

| Síntoma | Causa probable | Solución |
|---|---|---|
| `run.bat` dice "No se pudo arrancar" | Python 3.12 no instalado o no en PATH | Instalalo desde python.org marcando "Add to PATH" |
| Mensaje "Requiere Python 3.12.x" al arrancar | Versión incorrecta de Python en PATH | Usá `run.bat` o `py -3.12 run.py` explícitamente |
| Falla la instalación de dependencias | Sin internet / proxy corporativo | Verificá la conexión y reintentá `setup.bat` |
