"""Integración BYMADATA: fuentes de cotizaciones (open / realtime) que reemplazan
a Data912 en el hot-path, y enriquecimiento de ISIN del catálogo.

Portado de los clientes verificados en `BYMADATA_EXCEL/` (open.bymadata.com.ar +
addin.bymadata.com.ar). Ambas APIs comparten backend, campos y códigos de plazo;
solo cambian latencia (open ~20min vs realtime), auth (público vs OAuth) y alcance.
"""
