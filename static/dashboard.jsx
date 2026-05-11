const { useState, useEffect, useRef, useCallback } = React;

// --- Utility Functions ---

const timeAgo = (dateStr) => {
    if (!dateStr) return "";
    const seconds = Math.floor((new Date() - new Date(dateStr)) / 1000);
    let interval = seconds / 31536000;
    if (interval > 1) return Math.floor(interval) + "y ago";
    interval = seconds / 2592000;
    if (interval > 1) return Math.floor(interval) + "mo ago";
    interval = seconds / 86400;
    if (interval > 1) return Math.floor(interval) + "d ago";
    interval = seconds / 3600;
    if (interval > 1) return Math.floor(interval) + "h ago";
    interval = seconds / 60;
    if (interval > 1) return Math.floor(interval) + "m ago";
    if (seconds < 0) return "just now";
    return Math.floor(seconds) + "s ago";
};

const getSeverityColor = (sev) => {
    if (!sev) return "bg-gray-200 text-gray-800";
    if (sev === "P1") return "bg-red-600 text-white";
    if (sev === "P2") return "bg-orange-500 text-white";
    if (sev === "P3") return "bg-yellow-400 text-gray-900";
    return "bg-gray-400 text-white";
};

const getIssueTypeColor = (type) => {
    if (!type) return "bg-gray-100 text-gray-800 border-gray-200";
    switch(type.toLowerCase()) {
        case "api_failure": return "bg-blue-100 text-blue-800 border-blue-200";
        case "auth_issue": return "bg-purple-100 text-purple-800 border-purple-200";
        case "data_mismatch": return "bg-teal-100 text-teal-800 border-teal-200";
        case "payment_failure": return "bg-red-100 text-red-800 border-red-200";
        default: return "bg-gray-100 text-gray-800 border-gray-200";
    }
};

const getConfidenceColor = (conf) => {
    if (!conf) return "bg-gray-100 text-gray-800 border-gray-200";
    const c = conf.toLowerCase();
    if (c === "high") return "bg-green-100 text-green-800 border-green-200";
    if (c === "medium") return "bg-yellow-100 text-yellow-800 border-yellow-200";
    if (c === "low") return "bg-red-100 text-red-800 border-red-200";
    return "bg-gray-100 text-gray-800 border-gray-200";
};

const Toast = ({ message, type }) => {
    if (!message) return null;
    const bg = type === 'error' ? 'bg-red-600' : 'bg-green-600';
    return (
        <div className={`fixed bottom-4 right-4 ${bg} text-white px-4 py-2 rounded shadow-lg z-50 animate-bounce`}>
            {message}
        </div>
    );
};

// --- Main Application ---

function App() {
    const [stats, setStats] = useState(null);
    const [cases, setCases] = useState([]);
    const [selectedCaseId, setSelectedCaseId] = useState(null);
    const [caseDetail, setCaseDetail] = useState(null);
    const [filter, setFilter] = useState('draft_ready');
    const [lastRefreshed, setLastRefreshed] = useState(new Date());
    const [refreshInterval, setRefreshInterval] = useState(0); // seconds since refresh
    
    const [draftBody, setDraftBody] = useState("");
    const [savingDraft, setSavingDraft] = useState(false);
    const [draftSaved, setDraftSaved] = useState(false);
    const [isSending, setIsSending] = useState(false);
    const [isEscalating, setIsEscalating] = useState(false);
    
    const [toast, setToast] = useState({ message: null, type: null });

    const showToast = (message, type = 'success') => {
        setToast({ message, type });
        setTimeout(() => setToast({ message: null, type: null }), 3000);
    };

    const fetchStats = async () => {
        try {
            const res = await fetch('/api/stats');
            const data = await res.json();
            setStats(data);
        } catch (e) {
            console.error(e);
        }
    };

    const fetchCases = useCallback(async () => {
        try {
            const res = await fetch(`/api/cases?status=${filter}&limit=50`);
            const data = await res.json();
            setCases(data.cases || []);
            setLastRefreshed(new Date());
            setRefreshInterval(0);
        } catch (e) {
            console.error(e);
        }
    }, [filter]);

    const fetchCaseDetail = async (id) => {
        try {
            const res = await fetch(`/api/cases/${id}`);
            const data = await res.json();
            setCaseDetail(data);
            if (data.draft) {
                setDraftBody(data.draft.draft_body || "");
            } else {
                setDraftBody("");
            }
        } catch (e) {
            console.error(e);
        }
    };

    // Auto refresh queue
    useEffect(() => {
        fetchCases();
        fetchStats();
        
        const interval = setInterval(() => {
            fetchCases();
            fetchStats();
        }, 30000); // 30 seconds
        
        const timer = setInterval(() => {
            setRefreshInterval(prev => prev + 1);
        }, 1000);
        
        return () => {
            clearInterval(interval);
            clearInterval(timer);
        };
    }, [fetchCases]);

    // Fetch case details when selected
    useEffect(() => {
        if (selectedCaseId) {
            fetchCaseDetail(selectedCaseId);
        } else {
            setCaseDetail(null);
        }
    }, [selectedCaseId]);

    // Debounce draft save
    const saveDraftTimer = useRef(null);
    const handleDraftChange = (e) => {
        const val = e.target.value;
        setDraftBody(val);
        setSavingDraft(true);
        setDraftSaved(false);

        if (saveDraftTimer.current) clearTimeout(saveDraftTimer.current);
        
        saveDraftTimer.current = setTimeout(async () => {
            if (!selectedCaseId) return;
            try {
                const res = await fetch(`/api/cases/${selectedCaseId}/draft`, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ draft_body: val })
                });
                if (res.ok) {
                    setDraftSaved(true);
                }
            } catch (err) {
                console.error("Failed to save draft", err);
            } finally {
                setSavingDraft(false);
            }
        }, 1000);
    };

    const handleSendReply = async () => {
        if (!selectedCaseId) return;
        setIsSending(true);
        try {
            const res = await fetch(`/api/cases/${selectedCaseId}/approve`, {
                method: 'POST'
            });
            const data = await res.json();
            if (res.ok) {
                showToast("Reply sent successfully!");
                // Update local case state
                setCases(prev => prev.map(c => c.id === selectedCaseId ? { ...c, status: 'sent' } : c));
                fetchStats();
                setTimeout(() => setSelectedCaseId(null), 2000);
            } else {
                showToast(data.error || data.detail || "Failed to send reply", "error");
            }
        } catch (e) {
            showToast("Failed to send reply", "error");
        } finally {
            setIsSending(false);
        }
    };

    const handleEscalate = async () => {
        if (!selectedCaseId) return;
        const note = prompt("Optional escalation note:");
        if (note === null) return; // cancelled
        
        setIsEscalating(true);
        try {
            const res = await fetch(`/api/cases/${selectedCaseId}/escalate`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ note })
            });
            if (res.ok) {
                showToast("Case escalated");
                setCases(prev => prev.map(c => c.id === selectedCaseId ? { ...c, status: 'escalated' } : c));
                fetchStats();
                setTimeout(() => setSelectedCaseId(null), 1000);
            } else {
                showToast("Failed to escalate", "error");
            }
        } catch (e) {
            showToast("Failed to escalate", "error");
        } finally {
            setIsEscalating(false);
        }
    };

    return (
        <div className="flex flex-col h-full">
            {/* Header */}
            <header className="bg-white border-b border-gray-200 px-6 py-3 flex items-center justify-between shrink-0">
                <div className="flex items-center space-x-6">
                    <h1 className="text-xl font-bold text-gray-800">Support Agent</h1>
                    {stats && (
                        <div className="flex space-x-3 text-sm">
                            <span className="flex items-center"><span className="w-2 h-2 rounded-full bg-yellow-400 mr-2"></span> Draft Ready: {stats.draft_ready || 0}</span>
                            <span className="flex items-center"><span className="w-2 h-2 rounded-full bg-blue-400 mr-2"></span> Pending Info: {stats.pending_info || 0}</span>
                            <span className="flex items-center"><span className="w-2 h-2 rounded-full bg-orange-400 mr-2"></span> Investigating: {stats.investigating || 0}</span>
                            <span className="flex items-center"><span className="w-2 h-2 rounded-full bg-green-500 mr-2"></span> Sent Today: {stats.sent_today || 0}</span>
                        </div>
                    )}
                </div>
                <div className="flex items-center space-x-4">
                    <span className="text-xs text-gray-500">Refreshed {refreshInterval}s ago</span>
                    <button onClick={fetchCases} className="p-1.5 rounded bg-gray-100 hover:bg-gray-200 text-gray-600 transition" title="Refresh">
                        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path></svg>
                    </button>
                </div>
            </header>

            <div className="flex flex-1 overflow-hidden">
                {/* Left Column: Queue */}
                <div className="w-2/5 border-r border-gray-200 bg-gray-50 flex flex-col">
                    <div className="p-3 border-b border-gray-200 bg-white flex space-x-2 shrink-0 overflow-x-auto">
                        {['all', 'draft_ready', 'pending_info', 'sent'].map(tab => (
                            <button
                                key={tab}
                                onClick={() => setFilter(tab)}
                                className={`px-3 py-1.5 text-sm rounded-full font-medium transition ${filter === tab ? 'bg-gray-800 text-white' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'}`}
                            >
                                {tab.replace('_', ' ').replace(/\b\w/g, l => l.toUpperCase())}
                            </button>
                        ))}
                    </div>
                    <div className="flex-1 overflow-y-auto p-3 space-y-3">
                        {cases.length === 0 ? (
                            <div className="text-center text-gray-500 mt-10">No cases</div>
                        ) : (
                            cases.map(c => (
                                <div 
                                    key={c.id}
                                    onClick={() => setSelectedCaseId(c.id)}
                                    className={`bg-white rounded-lg shadow-sm border p-4 cursor-pointer transition ${selectedCaseId === c.id ? 'border-l-4 border-l-blue-500 border-gray-200' : 'border-gray-200 hover:border-blue-300'}`}
                                >
                                    <div className="flex justify-between items-start mb-2">
                                        <div className="flex items-center space-x-2">
                                            <span className={`px-2 py-0.5 text-xs rounded-md font-bold ${getSeverityColor(c.severity)}`}>{c.severity || 'Unk'}</span>
                                            <span className={`px-2 py-0.5 text-xs rounded-full border ${getIssueTypeColor(c.issue_type)}`}>{c.issue_type ? c.issue_type.replace('_', ' ') : 'unknown'}</span>
                                        </div>
                                        <span className="text-xs text-gray-400">{timeAgo(c.created_at)}</span>
                                    </div>
                                    <h3 className="font-semibold text-gray-800 truncate mb-1">{c.subject || 'No Subject'}</h3>
                                    <p className="text-sm text-gray-500 truncate mb-3">{c.client_email}</p>
                                    
                                    {c.status === 'draft_ready' && (
                                        <div className="flex justify-between items-center">
                                            <span className={`px-2 py-0.5 text-xs rounded-full border ${getConfidenceColor(c.confidence)}`}>
                                                Conf: {c.confidence || 'unknown'}
                                            </span>
                                            <span className="text-xs text-gray-500">Draft ready</span>
                                        </div>
                                    )}
                                </div>
                            ))
                        )}
                    </div>
                </div>

                {/* Right Column: Review Panel */}
                <div className="w-3/5 bg-white flex flex-col relative overflow-hidden">
                    {!caseDetail ? (
                        <div className="flex-1 flex flex-col items-center justify-center text-gray-400">
                            <svg className="w-16 h-16 mb-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.5" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"></path></svg>
                            <p className="text-lg">Select a case to review</p>
                        </div>
                    ) : (
                        <div className="flex-1 overflow-y-auto p-6">
                            {/* Case Header */}
                            <div className="mb-6">
                                <div className="flex items-center space-x-3 mb-2">
                                    <span className={`px-2.5 py-1 text-sm rounded-md font-bold ${getSeverityColor(caseDetail.severity)}`}>{caseDetail.severity || 'Unk'}</span>
                                    <h2 className="text-2xl font-bold text-gray-900 leading-tight">{caseDetail.subject || '(No Subject)'}</h2>
                                </div>
                                <div className="flex items-center text-sm text-gray-600 space-x-4">
                                    <span>From: <span className="font-medium text-gray-800">{caseDetail.client_email}</span></span>
                                    <span>•</span>
                                    <span className={`px-2 py-0.5 rounded-full border ${getIssueTypeColor(caseDetail.issue_type)}`}>{caseDetail.issue_type}</span>
                                    <span>•</span>
                                    <span className="uppercase tracking-wide font-semibold text-gray-500">{caseDetail.status.replace('_', ' ')}</span>
                                    <span>•</span>
                                    <span>{new Date(caseDetail.created_at).toLocaleString()}</span>
                                </div>
                            </div>

                            {/* Original Email */}
                            <details className="mb-6 group border border-gray-200 rounded-lg shadow-sm" open>
                                <summary className="px-4 py-3 bg-gray-50 font-medium text-gray-700 cursor-pointer select-none border-b border-gray-200 group-open:rounded-b-none rounded-lg flex items-center justify-between">
                                    Original Email
                                    <svg className="w-4 h-4 text-gray-500 transition-transform group-open:rotate-180" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M19 9l-7 7-7-7"></path></svg>
                                </summary>
                                <div className="p-4 bg-white">
                                    <div className="bg-gray-50 p-3 rounded text-sm font-mono whitespace-pre-wrap text-gray-800 overflow-y-auto max-h-48 border border-gray-200">
                                        {caseDetail.raw_body || "No content."}
                                    </div>
                                    {caseDetail.attachment_text && (
                                        <details className="mt-3 group/att border border-gray-200 rounded">
                                            <summary className="px-3 py-2 bg-gray-50 text-sm font-medium text-gray-600 cursor-pointer">Extracted attachment content</summary>
                                            <div className="p-3 bg-white text-xs font-mono whitespace-pre-wrap border-t border-gray-200 max-h-32 overflow-y-auto">
                                                {caseDetail.attachment_text}
                                            </div>
                                        </details>
                                    )}
                                </div>
                            </details>

                            {/* Agent Findings */}
                            {caseDetail.draft && caseDetail.draft.evidence && (
                                <details className="mb-6 group border border-gray-200 rounded-lg shadow-sm">
                                    <summary className="px-4 py-3 bg-indigo-50 font-medium text-indigo-900 cursor-pointer select-none border-b border-indigo-100 group-open:rounded-b-none rounded-lg flex items-center justify-between">
                                        Agent Findings
                                        <svg className="w-4 h-4 text-indigo-500 transition-transform group-open:rotate-180" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M19 9l-7 7-7-7"></path></svg>
                                    </summary>
                                    <div className="p-4 bg-white space-y-4">
                                        {caseDetail.draft.evidence.root_cause_summary && (
                                            <div className="bg-yellow-50 border-l-4 border-yellow-400 p-3 text-sm text-yellow-900">
                                                <strong>Root Cause Summary: </strong>
                                                {caseDetail.draft.evidence.root_cause_summary}
                                            </div>
                                        )}
                                        
                                        <div className="flex space-x-3">
                                            {caseDetail.draft.evidence.confidence && (
                                                <span className={`px-2.5 py-1 text-xs rounded-full border ${getConfidenceColor(caseDetail.draft.evidence.confidence)}`}>
                                                    Confidence: <span className="font-bold">{caseDetail.draft.evidence.confidence}</span>
                                                </span>
                                            )}
                                            {caseDetail.draft.evidence.suggested_action && (
                                                <span className="px-2.5 py-1 text-xs rounded-full border bg-gray-100 text-gray-700 border-gray-200">
                                                    Action: <span className="font-bold">{caseDetail.draft.evidence.suggested_action}</span>
                                                </span>
                                            )}
                                        </div>

                                        {caseDetail.draft.evidence.search_queries && caseDetail.draft.evidence.search_queries.length > 0 && (
                                            <div>
                                                <h4 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">Queries</h4>
                                                <div className="flex flex-wrap gap-1">
                                                    {caseDetail.draft.evidence.search_queries.map((q, i) => (
                                                        <span key={i} className="px-2 py-1 bg-gray-100 text-gray-600 text-xs rounded font-mono">{q}</span>
                                                    ))}
                                                </div>
                                            </div>
                                        )}

                                        {caseDetail.draft.evidence.github_code_results && caseDetail.draft.evidence.github_code_results.length > 0 && (
                                            <div>
                                                <h4 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">Code Snippets</h4>
                                                <div className="space-y-2">
                                                    {caseDetail.draft.evidence.github_code_results.map((res, i) => (
                                                        <div key={i} className="border rounded bg-gray-50 p-2">
                                                            <a href={res.html_url} target="_blank" className="text-xs text-blue-600 hover:underline mb-1 block truncate">{res.file_path}</a>
                                                            <pre className="text-[10px] font-mono text-gray-800 bg-white p-2 rounded border overflow-x-auto">{res.matched_lines}</pre>
                                                        </div>
                                                    ))}
                                                </div>
                                            </div>
                                        )}

                                        {caseDetail.draft.evidence.github_commits && caseDetail.draft.evidence.github_commits.length > 0 && (
                                            <div>
                                                <h4 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">Commits</h4>
                                                <div className="space-y-2">
                                                    {caseDetail.draft.evidence.github_commits.map((cmt, i) => (
                                                        <div key={i} className="border rounded p-2 text-xs">
                                                            <div className="flex justify-between items-start">
                                                                <a href={cmt.html_url} target="_blank" className="font-mono text-blue-600 hover:underline">{cmt.sha ? cmt.sha.substring(0,7) : 'link'}</a>
                                                                <span className="text-gray-400">{new Date(cmt.committed_at).toLocaleDateString()}</span>
                                                            </div>
                                                            <p className="text-gray-800 mt-1">{cmt.message}</p>
                                                            <p className="text-gray-500 mt-1">by {cmt.author}</p>
                                                        </div>
                                                    ))}
                                                </div>
                                            </div>
                                        )}

                                        {caseDetail.agent_logs && caseDetail.agent_logs.length > 0 && (
                                            <div>
                                                <h4 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">Agent Timing</h4>
                                                <div className="text-xs text-gray-600 font-mono">
                                                    {caseDetail.agent_logs.map(log => `${log.step} ${(log.duration_ms / 1000).toFixed(1)}s`).join(' | ')}
                                                </div>
                                            </div>
                                        )}
                                    </div>
                                </details>
                            )}

                            {/* Draft Reply Area */}
                            {caseDetail.draft ? (
                                <div className="mb-6">
                                    <div className="flex justify-between items-end mb-2">
                                        <label className="block text-sm font-semibold text-gray-700">Draft Reply (editable)</label>
                                        <div className="text-xs text-gray-400 flex items-center h-4">
                                            {savingDraft && <span className="text-yellow-500">Saving...</span>}
                                            {draftSaved && !savingDraft && <span className="text-green-500">Saved</span>}
                                        </div>
                                    </div>
                                    <textarea
                                        className="w-full border border-gray-300 rounded-lg p-3 text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 resize-y"
                                        rows={Math.max(8, draftBody.split('\n').length)}
                                        value={draftBody}
                                        onChange={handleDraftChange}
                                        disabled={caseDetail.status === 'sent'}
                                    />
                                    <div className="text-right text-xs text-gray-400 mt-1">{draftBody.length} chars</div>
                                </div>
                            ) : (
                                <div className="mb-6 p-4 bg-gray-50 border border-gray-200 rounded text-center text-sm text-gray-500">
                                    No draft available for this case.
                                </div>
                            )}

                            {/* Action Buttons */}
                            <div className="flex space-x-3 pt-4 border-t border-gray-200">
                                <button
                                    onClick={handleSendReply}
                                    disabled={!draftBody || isSending || isEscalating || caseDetail.status === 'sent'}
                                    className="flex-1 bg-green-600 hover:bg-green-700 disabled:bg-green-300 text-white font-semibold py-2.5 px-4 rounded-lg transition flex justify-center items-center"
                                >
                                    {isSending ? (
                                        <svg className="animate-spin h-5 w-5 text-white" fill="none" viewBox="0 0 24 24"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path></svg>
                                    ) : caseDetail.status === 'sent' ? 'Already Sent' : 'Send Reply'}
                                </button>
                                <button
                                    onClick={handleEscalate}
                                    disabled={isSending || isEscalating || caseDetail.status === 'escalated'}
                                    className="px-6 bg-gray-200 hover:bg-gray-300 disabled:bg-gray-100 text-gray-800 font-medium py-2.5 rounded-lg transition"
                                >
                                    {isEscalating ? '...' : caseDetail.status === 'escalated' ? 'Escalated' : 'Escalate'}
                                </button>
                            </div>
                        </div>
                    )}
                </div>
            </div>
            <Toast message={toast.message} type={toast.type} />
        </div>
    );
}

const root = ReactDOM.createRoot(document.getElementById('root'));
root.render(<App />);
