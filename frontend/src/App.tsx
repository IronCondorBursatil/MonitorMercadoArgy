import { useState, useEffect } from 'react';
import './index.css';
import { DataGrid } from './components/DataGrid';

function App() {
  const [bonds, setBonds] = useState<any[]>([]);

  const fetchBonds = () => {
    fetch('/api/v1/market/bonares')
      .then(res => res.json())
      .then(data => setBonds(data))
      .catch(err => console.error(err));
  };

  useEffect(() => {
    // Initial fetch
    fetchBonds();

    // SSE Subscription
    const evtSource = new EventSource('/stream');
    evtSource.addEventListener('refresh', () => {
      // Refresh the data when a refresh event is received from the server
      fetchBonds();
    });
    evtSource.addEventListener('ping', () => {
      // Keep-alive
    });

    return () => {
      evtSource.close();
    };
  }, []);

  const columns = [
    { key: 'symbol', header: 'Especie' },
    { key: 'px_bid', header: 'Compra', isNumeric: true },
    { key: 'px_ask', header: 'Venta', isNumeric: true },
    { key: 'c', header: 'Último', isNumeric: true },
    { key: 'pct_change', header: 'Var %', isNumeric: true },
    { key: 'v', header: 'Volumen', isNumeric: true },
  ];

  return (
    <div className="app-container">
      <header className="top-bar">
        <h1>MONITOR · Renta Fija AR</h1>
      </header>
      
      <main className="dashboard-grid">
        <div className="panel">
          <div className="panel-header">Bonos Soberanos</div>
          <div className="panel-body">
            <DataGrid data={bonds} columns={columns} />
          </div>
        </div>
      </main>
    </div>
  );
}

export default App;
