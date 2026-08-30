

interface Column {
  key: string;
  header: string;
  isNumeric?: boolean;
}

interface DataGridProps {
  data: any[];
  columns: Column[];
}

export const DataGrid: React.FC<DataGridProps> = ({ data, columns }) => {
  return (
    <table className="data-grid">
      <thead>
        <tr>
          {columns.map(col => (
            <th key={col.key}>{col.header}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {data.map((row, idx) => (
          <tr key={row.id || idx}>
            {columns.map(col => {
              const value = row[col.key];
              const isNum = col.isNumeric;
              // Format numeric values
              let displayVal = value;
              if (isNum && typeof value === 'number') {
                displayVal = value.toLocaleString('es-AR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
              }

              // Color pos/neg
              let className = isNum ? 'num' : '';
              if (col.key.includes('pct') || col.key.includes('change')) {
                if (value > 0) className += ' pos';
                if (value < 0) className += ' neg';
              }

              return (
                <td key={col.key} className={className.trim()}>
                  {displayVal}
                </td>
              );
            })}
          </tr>
        ))}
      </tbody>
    </table>
  );
};
