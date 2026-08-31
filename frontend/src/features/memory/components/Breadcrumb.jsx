import React, { useState } from 'react';
import { ChevronRight, Home, Copy, Check } from 'lucide-react';
import clsx from 'clsx';

const Breadcrumb = ({ items = [], onNavigate }) => {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    const path = items.length > 0 ? items[items.length - 1].path : '';
    try {
      await navigator.clipboard.writeText(path);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // fallback
      const ta = document.createElement('textarea');
      ta.value = path;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }
  };

  return (
    <div className="flex items-center gap-2 overflow-x-auto no-scrollbar mask-linear-fade">
      <button 
        onClick={() => onNavigate('')}
        className="p-1.5 rounded-md hover:bg-slate-800/50 text-slate-500 hover:text-indigo-400 transition-colors"
      >
        <Home size={14} />
      </button>
      
      {items.map((crumb, i) => (
        <React.Fragment key={crumb.path}>
          <ChevronRight size={12} className="text-slate-700 flex-shrink-0" />
          <button
            onClick={() => onNavigate(crumb.path)}
            className={clsx(
              "px-2 py-1 rounded-md text-xs font-medium transition-all whitespace-nowrap",
              i === items.length - 1
                ? "bg-indigo-500/10 text-indigo-300 border border-indigo-500/20"
                : "text-slate-400 hover:text-slate-200 hover:bg-white/5"
            )}
          >
            {crumb.label}
          </button>
        </React.Fragment>
      ))}
      <button
        onClick={handleCopy}
        className="p-1.5 rounded-md hover:bg-slate-800/50 text-slate-600 hover:text-slate-300 transition-colors flex-shrink-0"
        title={copied ? 'Copied!' : 'Copy path'}
      >
        {copied ? <Check size={14} className="text-green-400" /> : <Copy size={14} />}
      </button>
    </div>
  );
};

export default Breadcrumb;
