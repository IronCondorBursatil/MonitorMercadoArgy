/* Sectores ON: orden canónico + paleta + íconos. Compartido por los 20 mockups.
   El campo `sector` ya viene calculado por bono en on_data.js (build_on_mockup_data.py).
   Acá solo definimos color/orden/etiqueta-corta para pintar consistente. */
window.ON_SECTORS = [
  { key: "Energía / Petróleo & Gas",        short: "Energía",    color: "#ED8B36", icon: "⚡" },
  { key: "Utilities (Luz / Gas)",            short: "Utilities",  color: "#2FA4C9", icon: "🔌" },
  { key: "Servicios Financieros",            short: "Serv. Financieros", color: "#6C5CE0", icon: "🏦" },
  { key: "Agro / Alimentos",                 short: "Agro",       color: "#2FB36B", icon: "🌾" },
  { key: "Industrial / Maquinaria",          short: "Industrial", color: "#C0573C", icon: "⚙️" },
  { key: "Infraestructura / Construcción",   short: "Infra",      color: "#C9A227", icon: "🏗️" },
  { key: "Real Estate",                      short: "Real Estate",color: "#D85FA0", icon: "🏢" },
  { key: "Telecomunicaciones",               short: "Telco",      color: "#0E9C8A", icon: "📡" },
  { key: "Salud / Farma",                     short: "Salud",      color: "#E0566E", icon: "💊" },
  { key: "Minería",                          short: "Minería",    color: "#8D6E63", icon: "⛏️" },
  { key: "Otros",                            short: "Otros",      color: "#8993B8", icon: "•" },
];
window.ON_SECTOR_MAP = Object.fromEntries(window.ON_SECTORS.map(s => [s.key, s]));
