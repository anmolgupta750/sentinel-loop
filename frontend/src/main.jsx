import { useEffect, useRef, useState } from "react";
import {
	Activity,
	AlertCircle,
	ArrowRight,
	Calculator,
	Check,
	CheckCircle2,
	ChevronDown,
	ChevronRight,
	Circle,
	Code2,
	Database,
	FileText,
	Layers,
	LockKeyhole,
	MessageSquare,
	Play,
	Plus,
	RefreshCw,
	RotateCcw,
	Search,
	ShieldAlert,
	ShieldCheck,
	Sparkles,
	Trash2,
	Upload,
	UserCheck,
	UserRound,
	Wrench,
	X,
	XCircle,
} from "lucide-react";
import "./styles.css";

const API = import.meta.env.VITE_API_URL || "http://localhost:8000/api";

function formatTime(isoString) {
	if (!isoString) return "";
	try {
		const d = new Date(isoString);
		return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
	} catch {
		return isoString;
	}
}

export default function App() {
	// Mode toggle: 'user' vs 'dev'
	const [viewMode, setViewMode] = useState("user");

	// Task & Workflow state
	const [taskDescription, setTaskDescription] = useState("");
	const [workflow, setWorkflow] = useState(null);
	const [auditEvents, setAuditEvents] = useState([]);
	const [expandedStepId, setExpandedStepId] = useState(null);
	const [expandedRagStepId, setExpandedRagStepId] = useState(null);
	const [expandedToolStepId, setExpandedToolStepId] = useState(null);
	const [expandedEvidenceStepId, setExpandedEvidenceStepId] = useState(null);
	const [showTrace, setShowTrace] = useState(false);
	const [showKb, setShowKb] = useState(false);

	// Approval & Revision note
	const [note, setNote] = useState("");
	const [showRejectInput, setShowRejectInput] = useState(false);
	const [loading, setLoading] = useState(false);
	const [error, setError] = useState("");

	// Knowledge Base state
	const [documents, setDocuments] = useState([]);
	const [selectedDocIds, setSelectedDocIds] = useState([]);
	const [uploadingDoc, setUploadingDoc] = useState(false);
	const fileInputRef = useRef(null);

	// Load knowledge documents
	async function loadDocuments() {
		try {
			const res = await fetch(`${API}/documents`);
			if (res.ok) {
				const data = await res.json();
				setDocuments(data.documents || []);
			}
		} catch (err) {
			console.error("Could not load documents", err);
		}
	}

	// Load audit trail
	async function loadAudit(taskId) {
		try {
			const res = await fetch(`${API}/audit/${taskId}`);
			if (res.ok) {
				const data = await res.json();
				setAuditEvents(data.events || []);
			}
		} catch {
			setAuditEvents([]);
		}
	}

	useEffect(() => {
		loadDocuments();
	}, []);

	// Polling while task is running
	useEffect(() => {
		if (!workflow?.task_id) return;
		loadAudit(workflow.task_id);

		if (workflow.status === "running") {
			const interval = setInterval(async () => {
				try {
					const res = await fetch(`${API}/tasks/${workflow.task_id}`);
					if (res.ok) {
						const updated = await res.json();
						setWorkflow(updated);
						loadAudit(workflow.task_id);
						if (updated.status !== "running") {
							clearInterval(interval);
						}
					}
				} catch (err) {
					console.error("Polling error", err);
				}
			}, 1500);

			return () => clearInterval(interval);
		}
	}, [workflow?.task_id, workflow?.status]);

	// Auto-expand current active step
	useEffect(() => {
		if (workflow?.steps?.length && expandedStepId === null) {
			setExpandedStepId(workflow.current_step || workflow.steps[0].id);
		}
	}, [workflow?.steps?.length]);

	// Upload document
	async function handleFileUpload(event) {
		const file = event.target.files?.[0];
		if (!file) return;
		setUploadingDoc(true);
		setError("");
		const formData = new FormData();
		formData.append("file", file);

		try {
			const res = await fetch(`${API}/documents`, { method: "POST", body: formData });
			if (!res.ok) throw new Error(await res.text());
			await loadDocuments();
			if (fileInputRef.current) fileInputRef.current.value = "";
		} catch (uploadError) {
			setError(`Upload failed: ${uploadError.message}`);
		} finally {
			setUploadingDoc(false);
		}
	}

	// Delete document
	async function handleDeleteDoc(docId, event) {
		event.stopPropagation();
		try {
			const res = await fetch(`${API}/documents/${docId}`, { method: "DELETE" });
			if (res.ok) {
				setSelectedDocIds((prev) => prev.filter((id) => id !== docId));
				await loadDocuments();
			}
		} catch (err) {
			setError(`Delete failed: ${err.message}`);
		}
	}

	// Toggle doc selection
	function toggleDocSelection(docId) {
		setSelectedDocIds((prev) =>
			prev.includes(docId) ? prev.filter((id) => id !== docId) : [...prev, docId]
		);
	}

	// Start task
	async function runTask() {
		if (!taskDescription.trim()) return;
		setLoading(true);
		setError("");
		try {
			const res = await fetch(`${API}/tasks`, {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({
					task_description: taskDescription,
					document_ids: selectedDocIds,
				}),
			});
			if (!res.ok) throw new Error(await res.text());
			const nextWf = await res.json();
			setWorkflow(nextWf);
			setExpandedStepId(1);
			setShowTrace(false);
		} catch (err) {
			setError(`Could not run task: ${err.message}`);
		} finally {
			setLoading(false);
		}
	}

	// Human decision (approve or request changes)
	async function handleDecision(approved) {
		if (!workflow) return;
		setLoading(true);
		setError("");
		try {
			const res = await fetch(`${API}/tasks/${workflow.task_id}/approval`, {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ approved, note }),
			});
			if (!res.ok) throw new Error(await res.text());
			setWorkflow(await res.json());
			setShowRejectInput(false);
		} catch (err) {
			setError(`Could not record decision: ${err.message}`);
		} finally {
			setLoading(false);
		}
	}

	// Authorize revision retry
	async function handleRetry() {
		if (!workflow) return;
		setLoading(true);
		setError("");
		try {
			const res = await fetch(`${API}/tasks/${workflow.task_id}/retry`, {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ note }),
			});
			if (!res.ok) throw new Error(await res.text());
			setWorkflow(await res.json());
			setNote("");
		} catch (err) {
			setError(`Could not retry: ${err.message}`);
		} finally {
			setLoading(false);
		}
	}

	const steps = workflow?.steps || [];
	const isRunning = workflow?.status === "running";
	const isAwaitingApproval = workflow?.status === "awaiting_approval";
	const isNeedsRetry = workflow?.status === "needs_retry";
	const isCompleted = workflow?.status === "completed";
	const isEscalated = workflow?.status === "escalated_to_human";

	return (
		<div className="product-shell">
			{/* Top Navbar */}
			<header className="product-topbar">
				<div className="topbar-inner">
					<div className="product-brand">
						<div className="brand-badge">
							<ShieldCheck size={18} />
						</div>
						<div className="brand-text">
							<span className="brand-title">SENTINEL LOOP</span>
							<span className="brand-sub">Autonomous Task Runtime</span>
						</div>
					</div>

					<div className="topbar-controls">
						<div className="view-mode-switch">
							<button
								className={`mode-btn ${viewMode === "user" ? "active" : ""}`}
								onClick={() => setViewMode("user")}
							>
								User View
							</button>
							<button
								className={`mode-btn ${viewMode === "dev" ? "active" : ""}`}
								onClick={() => setViewMode("dev")}
							>
								<Code2 size={13} /> Developer Trace
							</button>
						</div>
					</div>
				</div>
			</header>

			{/* Main Container */}
			<main className="product-main">
				{/* 1. Main Task Input Section */}
				<section className="card task-input-card">
					<div className="task-input-header">
						<label htmlFor="task-prompt" className="task-label">
							What do you want the agent to do?
						</label>
						<button
							className="kb-toggle-btn"
							onClick={() => setShowKb(!showKb)}
							title="Toggle Knowledge Base Documents"
						>
							<Database size={14} />
							{documents.length > 0 ? `${documents.length} Knowledge Docs` : "Knowledge Base"}
							{selectedDocIds.length > 0 && (
								<span className="kb-attached-pill">{selectedDocIds.length} attached</span>
							)}
						</button>
					</div>

					<textarea
						id="task-prompt"
						className="task-textarea"
						value={taskDescription}
						onChange={(e) => setTaskDescription(e.target.value)}
						placeholder="e.g. Using the attached document, summarize the key findings in 3 concise bullet points"
						rows="3"
					/>

					{/* Collapsible Knowledge Base Area */}
					{showKb && (
						<div className="kb-section">
							<div className="kb-section-top">
								<span className="kb-section-title">Knowledge Base Documents (RAG)</span>
								<input
									type="file"
									ref={fileInputRef}
									style={{ display: "none" }}
									accept=".txt,.text,.md,.markdown,.pdf"
									onChange={handleFileUpload}
								/>
								<button
									className="btn-secondary btn-sm"
									onClick={() => fileInputRef.current?.click()}
									disabled={uploadingDoc}
								>
									<Upload size={13} /> {uploadingDoc ? "Uploading..." : "Upload Document"}
								</button>
							</div>

							<div className="kb-chip-list">
								{documents.length > 0 ? (
									documents.map((doc) => {
										const isSelected = selectedDocIds.includes(doc.document_id);
										return (
											<div
												key={doc.document_id}
												className={`kb-doc-chip ${isSelected ? "selected" : ""}`}
												onClick={() => toggleDocSelection(doc.document_id)}
												title={`Click to ${isSelected ? "remove from" : "attach to"} current task`}
											>
												<FileText size={13} />
												<span className="doc-name">{doc.document_name}</span>
												<span className="doc-type">{doc.source_type}</span>
												<button
													className="doc-del-btn"
													onClick={(e) => handleDeleteDoc(doc.document_id, e)}
													title="Delete document"
												>
													<Trash2 size={12} />
												</button>
											</div>
										);
									})
								) : (
									<p className="kb-empty-hint">
										No documents uploaded. Upload PDF, Markdown, or TXT files to ground the agent in custom knowledge.
									</p>
								)}
							</div>
						</div>
					)}

					<div className="task-input-footer">
						<span className="task-subtext">
							Factual source-grounding active • Strict verifier checks factuality without ungrounded hallucinations.
						</span>
						<button
							className="btn-primary"
							onClick={runTask}
							disabled={loading || !taskDescription.trim() || isRunning}
						>
							{isRunning ? (
								<>
									<RefreshCw size={15} className="spin" /> Executing...
								</>
							) : (
								<>
									<Play size={15} fill="currentColor" /> Run Task
								</>
							)}
						</button>
					</div>
				</section>

				{error && (
					<div className="alert-banner">
						<AlertCircle size={16} />
						<span>{error}</span>
					</div>
				)}

				{/* 2. Active Task Progress & Step Cards */}
				{workflow && (
					<div className="workflow-container">
						{/* Overview Bar */}
						<div className="workflow-overview-card">
							<div className="overview-left">
								<div className="overview-kicker-row">
									<span className="overview-kicker">ACTIVE TASK</span>
									{workflow.grounding_required && (
										<span className="grounding-badge-top">
											<ShieldCheck size={11} /> Source-Grounding Required
										</span>
									)}
									{workflow.fabrication_allowed && (
										<span className="creative-badge-top">
											<Sparkles size={11} /> Creative Mode
										</span>
									)}
								</div>
								<h2 className="overview-task-text">{workflow.task_description}</h2>
							</div>
							<div className="overview-right">
								<div className={`status-tag ${workflow.status}`}>
									{isRunning && <RefreshCw size={12} className="spin" />}
									{isCompleted && <CheckCircle2 size={12} />}
									{isAwaitingApproval && <UserRound size={12} />}
									{isNeedsRetry && <RotateCcw size={12} />}
									{isEscalated && <AlertCircle size={12} />}
									<span>
										{workflow.status === "awaiting_approval"
											? "Awaiting Human Approval"
											: workflow.status === "needs_retry"
											? "Revision Requested"
											: workflow.status === "escalated_to_human"
											? "Escalated to Human"
											: workflow.status === "completed"
											? "Completed"
											: "Executing Plan"}
									</span>
								</div>
							</div>
						</div>

						{/* Manager Plan Summary */}
						<div className="manager-plan-badge">
							<Check size={14} className="check-icon" />
							<span>
								<strong>Manager:</strong> Plan created — {steps.length} step{steps.length > 1 ? "s" : ""}{" "}
								{workflow.grounding_required ? "(Factual Source Grounding Active)" : ""}
							</span>
						</div>

						{/* Vertical Timeline / Step Cards */}
						<div className="step-timeline">
							{steps.map((step) => {
								const isExpanded = expandedStepId === step.id;
								const isStepRunning = step.status === "running";
								const isPassed = step.verdict === "PASS" || step.status === "passed";
								const isFailed = step.verdict === "FAIL";
								const isWaiting = step.status === "queued";
								const hasUnsupportedClaims = step.unsupported_claims && step.unsupported_claims.length > 0;

								return (
									<div
										key={step.id}
										className={`step-card ${isExpanded ? "expanded" : ""} ${step.status}`}
									>
										{/* Step Card Header */}
										<div
											className="step-card-header"
											onClick={() => setExpandedStepId(isExpanded ? null : step.id)}
										>
											<div className="step-badge">
												{isPassed ? (
													<span className="icon-pass"><Check size={14} /></span>
												) : isStepRunning ? (
													<span className="icon-running"><RefreshCw size={13} className="spin" /></span>
												) : isFailed ? (
													<span className="icon-fail"><X size={14} /></span>
												) : (
													<span className="icon-wait"><Circle size={12} /></span>
												)}
											</div>

											<div className="step-header-info">
												<div className="step-header-top">
													<span className="step-num">STEP {step.id}</span>
													<span className="step-attempts">
														{step.attempts} attempt{step.attempts === 1 ? "" : "s"}
													</span>

													{/* Grounding Status Pill in User View */}
													{step.verdict && (
														step.criteria?.source_grounding ? (
															<span className="grounding-pill grounded" title="All concrete claims verified in sources">
																✓ Grounded
															</span>
														) : hasUnsupportedClaims ? (
															<span className="grounding-pill unsupported" title="Unsupported factual claims detected">
																✗ Unsupported Claims
															</span>
														) : (
															<span className="grounding-pill review">
																⚠ Needs Review
															</span>
														)
													)}
												</div>
												<div className="step-instruction-summary">{step.instruction}</div>
											</div>

											<div className="step-header-verdict">
												{isPassed && <span className="verdict-pill pass">✓ PASS</span>}
												{isFailed && <span className="verdict-pill fail">FAIL</span>}
												{isStepRunning && <span className="verdict-pill running">⟳ Running</span>}
												{isWaiting && <span className="verdict-pill wait">Waiting</span>}
												<ChevronDown
													size={16}
													className={`chevron-icon ${isExpanded ? "rotate" : ""}`}
												/>
											</div>
										</div>

										{/* Expanded Step Inspector */}
										{isExpanded && (
											<div className="step-inspector">
												{/* Instruction */}
												<div className="inspector-row">
													<span className="inspector-label">Step instruction:</span>
													<div className="inspector-value text-strong">{step.instruction}</div>
												</div>

												{/* RAG Information */}
												{step.rag_sources?.length > 0 && (
													<div className="rag-box">
														<div className="rag-box-header">
															<span className="rag-title">
																<Database size={13} /> RAG: {step.rag_chunks?.length || 1} relevant chunk{step.rag_chunks?.length > 1 ? "s" : ""} retrieved ({step.rag_sources.join(", ")})
															</span>
															<button
																className="btn-link"
																onClick={() =>
																	setExpandedRagStepId(
																		expandedRagStepId === step.id ? null : step.id
																	)
																}
															>
																{expandedRagStepId === step.id ? "Hide sources" : "View sources"}
															</button>
														</div>
														{expandedRagStepId === step.id && (
															<div className="rag-chunks-detail">
																{step.rag_chunks?.map((chunk, cIdx) => (
																	<div key={cIdx} className="chunk-item">
																		<span className="chunk-meta">Source: {chunk.document_name}</span>
																		<p className="chunk-text">{chunk.text}</p>
																	</div>
																))}
															</div>
														)}
													</div>
												)}

												{/* Tool Calls */}
												{step.tool_calls?.length > 0 && (
													<div className="tools-box">
														<div className="tools-box-header">
															<span className="tools-title">
																<Calculator size={13} /> Tool: {step.tool_calls.map((t) => t.tool).join(", ")}
															</span>
															<button
																className="btn-link"
																onClick={() =>
																	setExpandedToolStepId(
																		expandedToolStepId === step.id ? null : step.id
																	)
																}
															>
																{expandedToolStepId === step.id ? "Hide details" : "View details"}
															</button>
														</div>
														{expandedToolStepId === step.id && (
															<div className="tool-calls-detail">
																{step.tool_calls.map((tc, tIdx) => (
																	<div key={tIdx} className="tool-call-item">
																		<code>{tc.tool}</code> (<code>{tc.input}</code>) → <strong>{tc.result}</strong>
																	</div>
																))}
															</div>
														)}
													</div>
												)}

												{/* Worker Output */}
												<div className="inspector-row">
													<span className="inspector-label">Worker output:</span>
													<div className="worker-output-box">
														<p className="worker-text">{typeof step.output === "string" ? step.output : JSON.stringify(step.output, null, 2)}</p>
													</div>
												</div>

												{/* Verification Assessment */}
												{step.verdict && (
													<div className={`verification-card ${step.verdict === "PASS" ? "pass" : "fail"}`}>
														<div className="verification-header">
															<div className="verification-status-title">
																<ShieldCheck size={15} />
																<strong>{step.verdict === "PASS" ? "✓ VERIFIED" : "VERIFICATION CRITIQUE"}</strong>
															</div>
															{step.criteria && (
																<div className="criteria-badges">
																	<span className={`crit-badge ${step.criteria.instruction_match ? "pass" : "fail"}`}>
																		{step.criteria.instruction_match ? "✓" : "✕"} Instruction Match
																	</span>
																	<span className={`crit-badge ${step.criteria.factual_quality ? "pass" : "fail"}`}>
																		{step.criteria.factual_quality ? "✓" : "✕"} Factual Quality
																	</span>
																	<span className={`crit-badge ${step.criteria.source_grounding ? "pass" : "fail"}`}>
																		{step.criteria.source_grounding ? "✓" : "✕"} Source Grounded
																	</span>
																	<span className={`crit-badge ${step.criteria.format_compliance ? "pass" : "fail"}`}>
																		{step.criteria.format_compliance ? "✓" : "✕"} Format Compliant
																	</span>
																</div>
															)}
														</div>

														{/* Unsupported claims callout if failed grounding */}
														{hasUnsupportedClaims && (
															<div className="unsupported-claims-box">
																<div className="unsupported-title">
																	<ShieldAlert size={14} />
																	<strong>Unsupported Factual Claims Detected:</strong>
																</div>
																<ul className="unsupported-list">
																	{step.unsupported_claims.map((claim, idx) => (
																		<li key={idx}>{claim}</li>
																	))}
																</ul>
															</div>
														)}

														<p className="verification-feedback">{step.feedback}</p>

														{/* Evidence Tracking in Inspector */}
														{step.evidence && step.evidence.length > 0 && (
															<div className="evidence-section">
																<button
																	className="btn-link evidence-toggle"
																	onClick={() =>
																		setExpandedEvidenceStepId(
																			expandedEvidenceStepId === step.id ? null : step.id
																		)
																	}
																>
																	{expandedEvidenceStepId === step.id ? "Hide evidence tracking ▴" : "View evidence validation trace ▾"}
																</button>
																{expandedEvidenceStepId === step.id && (
																	<div className="evidence-table">
																		{step.evidence.map((ev, evIdx) => (
																			<div key={evIdx} className={`evidence-row ${ev.supported ? "supported" : "unsupported"}`}>
																				<div className="ev-claim">
																					<span className="ev-badge">{ev.supported ? "✓ Supported" : "✗ Unsupported"}</span>
																					<strong>{ev.claim}</strong>
																				</div>
																				{ev.evidence && <p className="ev-quote">Evidence: "{ev.evidence}" ({ev.source || "Context"})</p>}
																			</div>
																		))}
																	</div>
																)}
															</div>
														)}
													</div>
												)}

												{/* Developer Mode Raw JSON */}
												{viewMode === "dev" && (
													<div className="dev-raw-section">
														<span className="dev-raw-label">Raw Step State (LangGraph):</span>
														<pre className="dev-raw-pre">{JSON.stringify(step, null, 2)}</pre>
													</div>
												)}
											</div>
										)}
									</div>
								);
							})}
						</div>

						{/* 3. Final Synthesized Result */}
						{workflow.final_result && (
							<section className="card final-result-card">
								<div className="final-result-header">
									<div className="final-result-title">
										<CheckCircle2 size={18} className="final-icon" />
										<span>FINAL RESULT</span>
									</div>
									<div className="final-badges-group">
										{workflow.synthesis_grounded !== null && (
											<span className={`final-ground-badge ${workflow.synthesis_grounded ? "grounded" : "unverified"}`}>
												{workflow.synthesis_grounded ? "✓ Synthesis Grounded" : "⚠ Synthesis Ungrounded"}
											</span>
										)}
										<span className="final-badge">Synthesized</span>
									</div>
								</div>
								<div className="final-result-body">
									<p className="final-text">{workflow.final_result}</p>
								</div>
							</section>
						)}

						{/* 4. Human Approval Zone */}
						{isAwaitingApproval && (
							<section className="card human-approval-card">
								<div className="approval-header">
									<div className="approval-avatar">
										<UserCheck size={20} />
									</div>
									<div>
										<h3 className="approval-heading">Human Approval Required</h3>
										<p className="approval-subheading">
											The multi-agent workflow completed all steps. Please review the final result above to approve or request changes.
										</p>
									</div>
								</div>

								{showRejectInput ? (
									<div className="reject-form">
										<label className="reject-label">Specify requested changes / reviewer note:</label>
										<textarea
											className="reject-textarea"
											value={note}
											onChange={(e) => setNote(e.target.value)}
											placeholder="e.g. Please clarify the second point and add a disclaimer..."
											rows="2"
										/>
										<div className="approval-btn-group">
											<button
												className="btn-secondary"
												onClick={() => setShowRejectInput(false)}
												disabled={loading}
											>
												Cancel
											</button>
											<button
												className="btn-warning"
												onClick={() => handleDecision(false)}
												disabled={loading || !note.trim()}
											>
												Submit Change Request
											</button>
										</div>
									</div>
								) : (
									<div className="approval-btn-group">
										<button
											className="btn-secondary"
											onClick={() => setShowRejectInput(true)}
											disabled={loading}
										>
											Request Changes
										</button>
										<button
											className="btn-success"
											onClick={() => handleDecision(true)}
											disabled={loading}
										>
											<CheckCircle2 size={16} /> Approve Result
										</button>
									</div>
								)}
							</section>
						)}

						{/* 5. Revision Requested Zone */}
						{isNeedsRetry && (
							<section className="card revision-card">
								<div className="revision-header">
									<RotateCcw size={18} />
									<div>
										<strong>Revision Requested</strong>
										<p>{workflow.revision_note ? `Note: "${workflow.revision_note}"` : "Authorize re-synthesis with feedback."}</p>
									</div>
								</div>
								<div className="revision-actions">
									<button className="btn-primary" onClick={handleRetry} disabled={loading}>
										<RefreshCw size={14} /> Authorize Revision Re-synthesis
									</button>
								</div>
							</section>
						)}

						{/* 6. Completed Banner */}
						{isCompleted && (
							<div className="completed-banner">
								<CheckCircle2 size={18} />
								<div>
									<strong>Approved & Finalized</strong>
									<span>The synthesized response is cleared with human governance.</span>
								</div>
							</div>
						)}

						{/* 7. Escalated Banner */}
						{isEscalated && (
							<div className="escalated-banner">
								<AlertCircle size={18} />
								<div>
									<strong>Escalated to Human</strong>
									<span>A step exceeded maximum retry attempts or factual grounding constraints and is paused for manual review.</span>
								</div>
							</div>
						)}

						{/* 8. Execution Trace (Collapsible) */}
						<section className="card trace-card">
							<div
								className="trace-header"
								onClick={() => setShowTrace(!showTrace)}
							>
								<div className="trace-title">
									<Activity size={16} />
									<strong>Execution Trace</strong>
									<span className="trace-count">({auditEvents.length || workflow.events?.length || 0} events)</span>
								</div>
								<button className="btn-link trace-toggle-btn">
									{showTrace ? "Hide trace ▴" : "Show trace ▾"}
								</button>
							</div>

							{showTrace && (
								<div className="trace-list">
									{(auditEvents.length
										? auditEvents.map((ev, i) => ({
												id: `ev-${ev.id || i}`,
												time: formatTime(ev.timestamp),
												actor: ev.actor || "system",
												action: ev.action || ev.event_type,
												detail: ev.detail || ev.verdict || "Logged",
												level: ev.level || (ev.verdict === "FAIL" ? "warning" : "info"),
										  }))
										: (workflow.events || []).map((e) => ({
												id: e.id,
												time: formatTime(e.timestamp),
												actor: e.actor,
												action: e.action,
												detail: e.detail,
												level: e.level,
										  }))
									).map((event) => (
										<div key={event.id} className="trace-item">
											<span className="trace-time">{event.time}</span>
											<span className={`trace-actor ${event.actor}`}>{event.actor}</span>
											<span className="trace-action">{event.action.replace(".", " / ")}</span>
											<span className="trace-detail">{event.detail}</span>
										</div>
									))}
								</div>
							)}
						</section>

						{/* 9. Developer Mode Full State Inspector */}
						{viewMode === "dev" && (
							<section className="card dev-state-card">
								<div className="dev-state-header">
									<Code2 size={16} />
									<strong>Developer Trace — Full Workflow State & Grounding Metadata</strong>
								</div>
								<div className="dev-meta-summary">
									<div><strong>Grounding Required:</strong> {workflow.grounding_required ? "YES" : "NO"}</div>
									<div><strong>Fabrication Allowed:</strong> {workflow.fabrication_allowed ? "YES" : "NO"}</div>
									<div><strong>Synthesis Grounded:</strong> {workflow.synthesis_grounded ? "YES" : "NO"}</div>
								</div>
								<pre className="dev-state-pre">{JSON.stringify(workflow, null, 2)}</pre>
							</section>
						)}
					</div>
				)}
			</main>
		</div>
	);
}

import { createRoot } from "react-dom/client";
createRoot(document.getElementById("root")).render(<App />);
