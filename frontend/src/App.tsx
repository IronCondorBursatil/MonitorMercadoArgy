import { useState, useEffect } from 'react';
import './index.css';
import { DataGrid } from './components/DataGrid';

function App() {
  const [bonds, setBonds] = useState<any[]>([]);

  useEffect(() => {
    // We no longer need fetchBonds(). The server sends the initial state and updates 
    // directly through the SSE connection via the "market_data" event.
    
    // SSE Subscription to the new v1 stream
    const evtSource = new EventSource('/api/v1/stream/');
    
    evtSource.addEventListener('market_data', (e) => {
      try {
        const data = JSON.parse(e.data);
        setBonds(data);
      } catch (err) {
        console.error("Failed to parse market data", err);
      }
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
