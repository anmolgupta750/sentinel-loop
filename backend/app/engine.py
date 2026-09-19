from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict
from uuid import uuid4

from groq import Groq

try:
    from langgraph.graph import END, START, StateGraph
except ImportError:
    END = START = StateGraph = None

from .audit import AuditStore
from .config import settings
from .models import AuditEvent, Step, StepStatus, TaskRequest, Workflow
from .rag.ingest import DocumentIngester
from .rag.retriever import KnowledgeRetriever
from .tools.registry import tool_registry

logger = logging.getLogger(__name__)


class TaskState(TypedDict, total=False):
    task_id: str
    plan: list[dict[str, Any]]
    retry_count: int
    verifier_feedback: str


class ManagerWorkerEngine:
    """General-purpose Manager/Worker task engine with generic source-grounding, RAG, and LangGraph governance."""

    def __init__(self) -> None:
        self.workflows: dict[str, Workflow] = {}
        self.audit = AuditStore(settings.audit_db_path)
        self.ingester = DocumentIngester(settings.chroma_path)
        self.retriever = KnowledgeRetriever(settings.chroma_path)
        self.tools = tool_registry
        self.graph = self._build_graph()

    def _build_graph(self):
        if StateGraph is None:
            raise RuntimeError("LangGraph is required to start Sentinel Loop.")
        graph = StateGraph(TaskState)
        graph.add_node("manager", self._manager_node)
        graph.add_node("worker", self._worker_node)
        graph.add_node("verifier", self._verifier_node)
        graph.add_node("synthesize_result", self._synthesize_node)
        graph.add_node("escalate", self._escalate_node)
        graph.add_edge(START, "manager")
        graph.add_edge("manager", "worker")
        graph.add_edge("worker", "verifier")
        graph.add_conditional_edges(
            "verifier",
            self._verification_route,
            {"retry": "worker", "next": "worker", "synthesize": "synthesize_result", "escalate": "escalate"},
        )
        graph.add_edge("synthesize_result", END)
        graph.add_edge("escalate", END)
        return graph.compile()

    def create(self, request: TaskRequest, background: bool = True) -> Workflow:
        task_id = f"task-{uuid4().hex[:8]}"
        has_docs = bool(request.document_ids)
        workflow = Workflow(
            task_id=task_id,
            task_description=request.task_description,
            document_ids=request.document_ids or [],
            grounding_required=has_docs,
        )
        self.workflows[task_id] = workflow
        self._record_and_event(
            workflow,
            event_type="task_created",
            actor="manager",
            action="task.created",
            detail=f"Task accepted with {len(request.document_ids or [])} attached knowledge documents.",
            metadata={"document_ids": request.document_ids, "grounding_required": workflow.grounding_required},
        )

        def _execute():
            try:
                self.graph.invoke({"task_id": task_id, "retry_count": 0}, config={"recursion_limit": 100})
            except Exception as error:
                logger.exception(f"Workflow execution failed for task {task_id}: {error}")
                workflow.status = "failed"
                workflow.error_message = str(error)
                self._record_and_event(
                    workflow,
                    event_type="task_failed",
                    actor="system",
                    action="task.failed",
                    detail=f"Execution error: {error}",
                    level="warning",
                )

        if background:
            thread = threading.Thread(target=_execute, daemon=True)
            thread.start()
        else:
            _execute()

        return workflow

    def get(self, task_id: str) -> Workflow:
        if task_id not in self.workflows:
            raise KeyError(task_id)
        return self.workflows[task_id]

    def approve(self, task_id: str, approved: bool, note: str = "") -> Workflow:
        workflow = self.get(task_id)
        if workflow.status not in ["awaiting_approval", "escalated_to_human"]:
            return workflow
        if approved:
            workflow.status = "completed"
            workflow.revision_note = None
            self._record_and_event(
                workflow,
                event_type="human_approved",
                actor="human",
                action="approval.granted",
                detail=note or "Human approved the synthesized result.",
                level="success",
                metadata={"note": note},
            )
            self._record_and_event(
                workflow,
                event_type="task_completed",
                actor="manager",
                action="task.completed",
                detail="Workflow successfully finished with human approval.",
                level="success",
            )
        else:
            workflow.status = "needs_retry"
            workflow.revision_note = note
            self._record_and_event(
                workflow,
                event_type="human_rejected",
                actor="human",
                action="approval.rejected",
                detail=note or "Human requested changes to the synthesized result.",
                level="warning",
                metadata={"note": note},
            )
        self._audit_final(workflow)
        return workflow

    def retry(self, task_id: str, note: str = "") -> Workflow:
        workflow = self.get(task_id)
        if workflow.status != "needs_retry":
            return workflow
        workflow.status = "running"
        revision_instruction = note or workflow.revision_note or "Revise the synthesized result using the original task and prior outputs."
        workflow.revision_note = revision_instruction
        self._record_and_event(
            workflow,
            event_type="revision_started",
            actor="human",
            action="retry.authorized",
            detail=f"Synthesis revision authorized: {revision_instruction}",
            metadata={"revision_instruction": revision_instruction},
        )

        def _run_revision():
            try:
                outputs = "\n\n".join(f"Step {step.id} ({step.instruction}):\n{step.output}" for step in workflow.steps if step.output)
                prompt = (
                    f"Original task:\n{workflow.task_description}\n\n"
                    f"Approved step outputs:\n{outputs}\n\n"
                    f"Previous synthesized result:\n{workflow.final_result or 'None'}\n\n"
                    f"Human reviewer feedback / revision request:\n{revision_instruction}\n\n"
                    f"Please produce an updated, cohesive final answer directly addressing the reviewer's feedback. "
                    f"Do NOT invent new factual claims not present in the approved outputs."
                )
                revised_text = self._groq_text(
                    "Synthesize a revised, cohesive final answer for the original task incorporating reviewer feedback. Return only the final answer.",
                    prompt,
                    max_tokens=2048,
                )
                workflow.final_result = revised_text
                workflow.status = "awaiting_approval"
                self._record_and_event(
                    workflow,
                    event_type="approval_requested",
                    actor="manager",
                    action="task.awaiting_approval",
                    detail="Revised synthesis is ready and waiting for human approval.",
                    level="warning",
                )
                self._audit_final(workflow)
            except Exception as error:
                logger.exception(f"Revision failed for task {task_id}: {error}")
                workflow.status = "needs_retry"
                workflow.error_message = str(error)

        thread = threading.Thread(target=_run_revision, daemon=True)
        thread.start()
        return workflow

    def plan_task(self, task_description: str, has_attached_docs: bool = False) -> tuple[bool, bool, list[dict[str, Any]]]:
        """Decomposes task and infers generic grounding requirements."""
        system = (
            "You are an expert Project Manager agent. Your job is to decompose the user's high-level task into an ordered sequence "
            "of clear, actionable sub-task instructions for worker agents, and determine whether factual source grounding is required.\n\n"
            "RULES FOR GROUNDING INFERENCE:\n"
            "- 'grounding_required': Set to true if the task asks for summaries, factual extraction, data analysis, answers from documents, or objective technical facts. "
            "Set to false if the task is purely creative, fictional, or generic brainstorming.\n"
            "- 'fabrication_allowed': Set to true ONLY if the user explicitly requests creative fiction, imaginary worldbuilding, or invented scenarios.\n\n"
            "RULES FOR STEPS:\n"
            "1. Each 'instruction' must be an ACTION DIRECTIVE stating what the worker should draft, extract, analyze, or compute.\n"
            "2. NEVER write the final answer or drafted content into the instruction field.\n"
            "3. Return ONLY valid JSON with the structure:\n"
            "{\n"
            "  \"grounding_required\": true/false,\n"
            "  \"fabrication_allowed\": true/false,\n"
            "  \"steps\": [\n"
            "    {\"instruction\": \"action directive\", \"type\": \"text\", \"grounding_required\": true/false, \"fabrication_allowed\": true/false}\n"
            "  ]\n"
            "}"
        )
        try:
            raw = self._groq_text(system, f"Task to plan:\n{task_description}\nAttached documents present: {has_attached_docs}", max_tokens=2048)
            parsed = self._parse_json(raw)
            if isinstance(parsed, dict):
                g_req = bool(parsed.get("grounding_required", has_attached_docs))
                if has_attached_docs:
                    g_req = True
                f_allow = bool(parsed.get("fabrication_allowed", False))
                raw_steps = parsed.get("steps", [])
            elif isinstance(parsed, list):
                g_req = has_attached_docs
                f_allow = False
                raw_steps = parsed
            else:
                g_req = has_attached_docs
                f_allow = False
                raw_steps = []

            plan = []
            for item in raw_steps:
                instruction = str(item.get("instruction", "")).strip()
                step_type = str(item.get("type", "text")).lower().strip()
                s_ground = item.get("grounding_required", g_req)
                s_fab = item.get("fabrication_allowed", f_allow)
                if instruction:
                    plan.append({
                        "instruction": instruction,
                        "type": step_type if step_type in {"text", "image", "video", "doc"} else "text",
                        "grounding_required": s_ground,
                        "fabrication_allowed": s_fab,
                    })
            if plan:
                return g_req, f_allow, plan
        except Exception:
            logger.exception("Manager plan_task call failed; using a single generic fallback step.")
        return has_attached_docs, False, [{"instruction": "Complete the requested task as accurately and usefully as possible.", "type": "text", "grounding_required": has_attached_docs, "fabrication_allowed": False}]

    def execute_worker_step(
        self,
        workflow: Workflow,
        step: Step,
        prior_context: str,
        feedback: str = "",
    ) -> tuple[str, list[str], list[dict[str, Any]], list[dict[str, Any]]]:
        """Executes a worker step with source-grounding boundary and controlled tools."""
        if step.type in {"image", "video"}:
            return f"Step type '{step.type}' is not implemented in this text pass. No simulated generation performed.", [], [], []

        rag_sources: list[str] = []
        rag_chunks: list[dict[str, Any]] = []
        tool_calls: list[dict[str, Any]] = []

        # 1. RAG Retrieval if documents attached or task/step references source materials
        should_check_rag = bool(workflow.document_ids) or any(
            kw in f"{workflow.task_description} {step.instruction}".lower()
            for kw in ["document", "policy", "manual", "uploaded", "file", "knowledge", "reference", "source", "provided", "data", "report"]
        )
        rag_context_text = ""
        if should_check_rag:
            retrieval = self.retriever.retrieve(
                query=f"{workflow.task_description} {step.instruction}",
                n_results=3,
                document_ids=workflow.document_ids if workflow.document_ids else None,
            )
            if retrieval.has_results:
                rag_sources = retrieval.sources
                rag_chunks = [c.model_dump() for c in retrieval.chunks]
                rag_context_text = retrieval.formatted_context
                self._record_and_event(
                    workflow,
                    event_type="rag_retrieval",
                    actor="retriever",
                    action="rag.retrieved",
                    detail=f"Retrieved {len(retrieval.chunks)} chunks from {', '.join(retrieval.sources)}.",
                    step_id=step.id,
                    metadata={"sources": rag_sources, "chunk_ids": [c.chunk_id for c in retrieval.chunks]},
                )

        # 2. Tool invocations if applicable
        math_check = re.findall(r"(\d+[\s\+\-\*\/\^\%]+[\d\.\(\)\+\-\*\/\^\%\s]+)", step.instruction)
        if math_check and any(op in step.instruction for op in ["+", "-", "*", "/", "^", "calculate", "compute", "sum"]):
            for expr in math_check:
                if len(expr.strip()) > 2 and any(ch.isdigit() for ch in expr):
                    calc_record = self.tools.execute("calculator", expr.strip())
                    if not calc_record.error:
                        tool_calls.append(calc_record.model_dump())
                        self._record_and_event(
                            workflow,
                            event_type="tool_called",
                            actor="worker",
                            action="tool.calculator",
                            detail=f"Computed '{expr.strip()}' -> {calc_record.result}",
                            step_id=step.id,
                            metadata=calc_record.model_dump(),
                        )

        # 3. Build Worker Prompt with Generic Grounding Rules
        grounding_rules = (
            "GENERIC FACTUALITY & SOURCE-GROUNDING RULES:\n"
            "- When source material or context is supplied, you must treat it as the STRICT FACTUAL BOUNDARY.\n"
            "- NEVER introduce specific factual claims (such as names, dates, numbers, percentages, measurements, entities, technical specs, or results) not supported by the supplied sources, retrieved context, or prior verified outputs.\n"
            "- If the available source evidence is insufficient or silent on a point, explicitly state that the information is not provided in the source material rather than guessing or fabricating.\n"
        ) if step.grounding_required or workflow.grounding_required else (
            "GENERIC RULES: Generate accurate, well-crafted, and engaging content fulfilling the directive."
        )

        if step.fabrication_allowed or workflow.fabrication_allowed:
            grounding_rules = "CREATIVE MODE: You may freely invent fictional elements, creative ideas, and narrative details appropriate for the task."

        system = (
            f"You are a skilled Worker agent. You independently execute the given sub-task instruction.\n\n"
            f"{grounding_rules}\n\n"
            f"Produce the actual content requested by the directive. Do NOT repeat the instruction. Do NOT include meta-commentary."
        )

        prompt = f"Original task:\n{workflow.task_description}\n\nStep instruction to execute:\n{step.instruction}\n"
        if rag_context_text:
            prompt += f"\n{rag_context_text}\n"
        if tool_calls:
            tool_summary = "\n".join(f"Tool '{tc['tool']}' input: {tc['input']} -> result: {tc['result']}" for tc in tool_calls)
            prompt += f"\n--- VERIFIED TOOL RESULTS ---\n{tool_summary}\n--- END TOOL RESULTS ---\n"
        if prior_context.strip():
            prompt += f"\nPrior verified context / completed step outputs:\n{prior_context}\n"
        if feedback.strip():
            prompt += f"\nVerifier feedback from previous attempt (FIX THESE ISSUES / REMOVE UNSUPPORTED CLAIMS):\n{feedback}\n"
        prompt += "\nOutput the generated content for this step:"

        worker_output = self._groq_text(system, prompt, max_tokens=2048)
        return worker_output, rag_sources, rag_chunks, tool_calls

    def verify_step(
        self,
        step_instruction: str,
        worker_output: str,
        task_description: str = "",
        sources_context: str = "",
        grounding_required: bool = False,
        fabrication_allowed: bool = False,
    ) -> dict[str, Any]:
        """Generic strict verifier assessing directive satisfaction, factual truth, and source-grounding."""
        grounding_eval_text = (
            "SOURCE GROUNDING CRITERION (STRICT):\n"
            "This task requires source grounding. Evaluate whether the Worker output is fully supported by the supplied sources/context.\n"
            "Check:\n"
            "1. Did the Worker invent or hallucinate concrete facts (names, dates, figures, percentages, measurements, entities, results) not present in the source material?\n"
            "2. Does the output contradict supplied sources?\n"
            "3. If information was missing in the source, did the Worker properly acknowledge the limitation or did it invent details?\n"
            "4. If any concrete claim is unsupported by the provided source material, set 'source_grounding': false, set 'verdict': 'FAIL', list the unsupported claim in 'unsupported_claims', and explain in 'feedback' exactly what was unsupported.\n"
        ) if grounding_required and not fabrication_allowed else (
            "SOURCE GROUNDING CRITERION:\n"
            "Evaluate whether the content is coherent and truthful. If fictional/creative mode is permitted, allow creative inventions."
        )

        system = (
            "You are a strict, objective Verifier / Critic agent. Evaluate whether the Worker output successfully fulfills the step instruction.\n\n"
            f"{grounding_eval_text}\n\n"
            "Evaluation criteria:\n"
            "- instruction_match: true/false (does the worker output carry out the step directive?)\n"
            "- factual_quality: true/false (is the worker output coherent, plausible, and free of internal contradictions?)\n"
            "- source_grounding: true/false (is every factual claim in the worker output grounded in the supplied context? False if any unverified concrete facts/numbers/names are fabricated.)\n"
            "- format_compliance: true/false (is the worker output formatted appropriately for the instruction, e.g. text/bullets/paragraphs, without repeating prompt instructions or metadata?)\n\n"
            "IMPORTANT: The Worker output can be normal text/markdown unless specific formatting was requested. YOUR verification response must be a JSON object with this exact schema:\n"
            "{\n"
            "  \"verdict\": \"PASS\" or \"FAIL\",\n"
            "  \"feedback\": \"detailed explanation of why it passed or actionable critique of what to fix/remove\",\n"
            "  \"criteria\": {\n"
            "    \"instruction_match\": true/false,\n"
            "    \"factual_quality\": true/false,\n"
            "    \"source_grounding\": true/false,\n"
            "    \"format_compliance\": true/false\n"
            "  },\n"
            "  \"unsupported_claims\": [\"list of unsupported claims if any\"],\n"
            "  \"evidence\": [\n"
            "    {\"claim\": \"summary of claim\", \"supported\": true/false, \"source\": \"source name or context\", \"evidence\": \"quote or fact from context\"}\n"
            "  ]\n"
            "}"
        )

        prompt = (
            f"High-level task:\n{task_description}\n\n"
            f"Step instruction (directive):\n{step_instruction}\n\n"
            f"Supplied source material / context:\n{sources_context or 'None provided (evaluate general coherence/task compliance)'}\n\n"
            f"Worker output (content to evaluate):\n{worker_output}\n\n"
            "Evaluate the worker output against the criteria and return your JSON verification report:"
        )
        try:
            raw = self._groq_text(system, prompt, max_tokens=2048)
            parsed = self._parse_json(raw)
            verdict_raw = parsed.get("verdict")
            if isinstance(verdict_raw, bool):
                verdict = "PASS" if verdict_raw else "FAIL"
            else:
                verdict_str = str(verdict_raw or "FAIL").upper().strip()
                verdict = "PASS" if "PASS" in verdict_str or verdict_str == "TRUE" else "FAIL"

            crit = parsed.get("criteria", {})
            inst_match = bool(crit.get("instruction_match", verdict == "PASS"))
            fact_qual = bool(crit.get("factual_quality", verdict == "PASS"))
            source_ground = bool(crit.get("source_grounding", verdict == "PASS"))
            fmt_comp = bool(crit.get("format_compliance", verdict == "PASS"))

            unsupported = parsed.get("unsupported_claims", [])
            if not isinstance(unsupported, list):
                unsupported = [str(unsupported)] if unsupported else []

            # If any unsupported claims exist or source_grounding is false on a grounded task, enforce FAIL
            if grounding_required and not fabrication_allowed and (unsupported or not source_ground):
                verdict = "FAIL"
                source_ground = False

            feedback = str(parsed.get("feedback", parsed.get("reason", "Output evaluated.")))
            evidence = parsed.get("evidence", [])
            if not isinstance(evidence, list):
                evidence = []

            return {
                "verdict": verdict,
                "feedback": feedback,
                "criteria": {
                    "instruction_match": inst_match,
                    "factual_quality": fact_qual,
                    "source_grounding": source_ground,
                    "format_compliance": fmt_comp,
                },
                "unsupported_claims": unsupported,
                "evidence": evidence,
            }
        except Exception:
            logger.exception("Verifier call failed; treating step as failed.")
            return {
                "verdict": "FAIL",
                "feedback": "Verifier unavailable; human escalation required.",
                "criteria": {
                    "instruction_match": False,
                    "factual_quality": False,
                    "source_grounding": False,
                    "format_compliance": False,
                },
                "unsupported_claims": ["Unable to verify claims due to parser error"],
                "evidence": [],
            }

    def verify_synthesis_grounding(
        self,
        task_description: str,
        final_result: str,
        approved_step_outputs: str,
        sources_context: str = "",
        grounding_required: bool = False,
    ) -> bool:
        """Ensures final synthesized answer did not introduce new unsupported factual claims."""
        if not grounding_required:
            return True
        system = (
            "You are a strict Synthesis Verifier. Ensure the synthesized answer does NOT introduce new concrete factual claims "
            "(names, numbers, dates, statistics, organizations, specifications) that were not present in the approved step outputs or source context.\n\n"
            "Return ONLY JSON: {\"grounded\": true/false, \"unsupported_claims\": []}"
        )
        prompt = (
            f"Original task: {task_description}\n\n"
            f"Approved step outputs:\n{approved_step_outputs}\n\n"
            f"Sources context:\n{sources_context}\n\n"
            f"Final Synthesized Result:\n{final_result}\n"
        )
        try:
            raw = self._groq_text(system, prompt, max_tokens=1024)
            parsed = self._parse_json(raw)
            return bool(parsed.get("grounded", True))
        except Exception:
            return True

    def _manager_node(self, state: TaskState) -> TaskState:
        workflow = self._workflow(state)
        g_req, f_allow, plan = self.plan_task(workflow.task_description, has_attached_docs=bool(workflow.document_ids))
        workflow.grounding_required = g_req
        workflow.fabrication_allowed = f_allow
        workflow.steps = [
            Step(
                id=index + 1,
                instruction=item["instruction"],
                type=item["type"],
                grounding_required=item.get("grounding_required", g_req),
                fabrication_allowed=item.get("fabrication_allowed", f_allow),
            )
            for index, item in enumerate(plan)
        ]
        workflow.current_step = 1
        self._record_and_event(
            workflow,
            event_type="manager_planned",
            actor="manager",
            action="plan.created",
            detail=f"Manager generated {len(plan)} ordered steps (Grounding Required: {'YES' if g_req else 'NO'}).",
            metadata={"step_count": len(plan), "grounding_required": g_req, "fabrication_allowed": f_allow},
        )
        return {**state, "plan": plan, "retry_count": 0, "verifier_feedback": ""}

    def _worker_node(self, state: TaskState) -> TaskState:
        workflow = self._workflow(state)
        step = workflow.steps[workflow.current_step - 1]
        step.status = StepStatus.running
        step.attempts += 1
        self._record_and_event(
            workflow,
            event_type="step_started",
            actor="worker",
            action="step.started",
            detail=f"Step {step.id} started (attempt {step.attempts}).",
            step_id=step.id,
        )

        prior_context = "\n\n".join(str(item.output) for item in workflow.steps[: workflow.current_step - 1] if item.output)
        feedback = state.get("verifier_feedback", "") if step.attempts > 1 else ""

        output, rag_sources, rag_chunks, tool_calls = self.execute_worker_step(workflow, step, prior_context, feedback)
        step.output = output
        step.rag_sources = rag_sources
        step.rag_chunks = rag_chunks
        step.tool_calls = tool_calls

        self._record_and_event(
            workflow,
            event_type="worker_completed",
            actor="worker",
            action="step.executed",
            detail=f"Worker executed step {step.id} (attempt {step.attempts}).",
            step_id=step.id,
            worker_output=output,
            metadata={"rag_sources": rag_sources, "tool_calls": tool_calls},
        )
        return state

    def _verifier_node(self, state: TaskState) -> TaskState:
        workflow = self._workflow(state)
        step = workflow.steps[workflow.current_step - 1]

        # Gather sources context for verifier
        sources_parts = []
        if step.rag_chunks:
            sources_parts.extend(f"[{c.get('document_name', 'Doc')}]: {c.get('text', '')}" for c in step.rag_chunks)
        if step.tool_calls:
            sources_parts.extend(f"Tool {tc.get('tool')}: {tc.get('input')} -> {tc.get('result')}" for tc in step.tool_calls)
        prior_outputs = [str(item.output) for item in workflow.steps[: workflow.current_step - 1] if item.output]
        if prior_outputs:
            sources_parts.append("Prior verified outputs:\n" + "\n\n".join(prior_outputs))

        sources_context = "\n\n".join(sources_parts)

        result = self.verify_step(
            step_instruction=step.instruction,
            worker_output=str(step.output),
            task_description=workflow.task_description,
            sources_context=sources_context,
            grounding_required=step.grounding_required or workflow.grounding_required,
            fabrication_allowed=step.fabrication_allowed or workflow.fabrication_allowed,
        )

        step.verdict = result["verdict"]
        step.feedback = result["feedback"]
        step.criteria = result.get("criteria", {})
        step.unsupported_claims = result.get("unsupported_claims", [])
        step.evidence = result.get("evidence", [])
        step.status = StepStatus.passed if result["verdict"] == "PASS" else StepStatus.retrying

        grounding_summary = "✓ Grounded" if step.criteria.get("source_grounding", True) else f"✗ {len(step.unsupported_claims)} unsupported claim(s)"

        self._record_and_event(
            workflow,
            event_type="verifier_completed",
            actor="verifier",
            action="step.verified",
            detail=f"Step {step.id}: {result['verdict']} ({grounding_summary}) - {result['feedback']}",
            step_id=step.id,
            verdict=result["verdict"],
            level="success" if result["verdict"] == "PASS" else "warning",
            metadata={
                "criteria": step.criteria,
                "unsupported_claims": step.unsupported_claims,
                "evidence": step.evidence,
            },
        )

        if result["verdict"] == "PASS":
            self._record_and_event(
                workflow,
                event_type="step_passed",
                actor="verifier",
                action="step.passed",
                detail=f"Step {step.id} successfully passed verification.",
                step_id=step.id,
                level="success",
            )
            return {**state, "verifier_feedback": "", "retry_count": 0}
        return {**state, "verifier_feedback": result["feedback"], "retry_count": step.attempts}

    def _synthesize_node(self, state: TaskState) -> TaskState:
        workflow = self._workflow(state)
        self._record_and_event(
            workflow,
            event_type="synthesis_started",
            actor="manager",
            action="synthesis.started",
            detail="Synthesizing final answer from approved step outputs.",
        )
        outputs = "\n\n".join(f"Step {step.id}: {step.output}" for step in workflow.steps)
        sources_text = "\n\n".join(
            f"[{chunk.get('document_name')}]: {chunk.get('text')}"
            for step in workflow.steps
            for chunk in step.rag_chunks
        )

        synthesizer_rules = (
            "SYNTHESIS RULES:\n"
            "1. Combine and clarify the approved outputs into a cohesive final answer.\n"
            "2. Do NOT invent new factual claims, figures, names, or statistics not present in the approved outputs or source context.\n"
            "3. Return ONLY the final answer."
        )

        workflow.final_result = self._groq_text(
            synthesizer_rules,
            f"Original task:\n{workflow.task_description}\n\nApproved outputs:\n{outputs}\n\nSource context:\n{sources_text}",
            max_tokens=2048,
        )

        # Final grounding check on synthesized output
        is_grounded = self.verify_synthesis_grounding(
            task_description=workflow.task_description,
            final_result=workflow.final_result,
            approved_step_outputs=outputs,
            sources_context=sources_text,
            grounding_required=workflow.grounding_required,
        )
        workflow.synthesis_grounded = is_grounded
        workflow.status = "awaiting_approval"
        workflow.current_step = len(workflow.steps)

        self._record_and_event(
            workflow,
            event_type="synthesis_completed",
            actor="manager",
            action="synthesis.completed",
            detail=f"Cohesive synthesis complete (Grounded: {'YES' if is_grounded else 'UNGROUNDED CLAIMS DETECTED'}).",
            level="info" if is_grounded else "warning",
            metadata={"synthesis_grounded": is_grounded},
        )
        self._record_and_event(
            workflow,
            event_type="approval_requested",
            actor="manager",
            action="task.awaiting_approval",
            detail="All planned steps passed. Final synthesis is waiting for human approval.",
            level="warning",
        )
        self._audit_final(workflow)
        return state

    def _escalate_node(self, state: TaskState) -> TaskState:
        workflow = self._workflow(state)
        step = workflow.steps[workflow.current_step - 1]
        step.status = StepStatus.escalated
        workflow.status = "escalated_to_human"
        self._record_and_event(
            workflow,
            event_type="step_escalated",
            actor="manager",
            action="task.escalated",
            detail=f"Step {step.id} failed verification three times (unsupported claims / criterion failure); paused for human review.",
            step_id=step.id,
            level="warning",
        )
        return state

    def _verification_route(self, state: TaskState) -> str:
        workflow = self._workflow(state)
        step = workflow.steps[workflow.current_step - 1]
        if step.verdict == "FAIL":
            if step.attempts < 3:
                self._record_and_event(
                    workflow,
                    event_type="retry_started",
                    actor="manager",
                    action="step.retry",
                    detail=f"Step {step.id} will retry (attempt {step.attempts + 1}) with verifier critique and grounding feedback.",
                    step_id=step.id,
                    level="warning",
                )
                return "retry"
            return "escalate"
        if workflow.current_step < len(workflow.steps):
            workflow.current_step += 1
            return "next"
        return "synthesize"

    def _groq_text(self, system: str, user: str, max_tokens: int = 4096) -> str:
        models = [settings.groq_model]
        for fallback in ["openai/gpt-oss-120b", "qwen/qwen3.8-27b"]:
            if fallback not in models:
                models.append(fallback)

        last_error = None
        for current_model in models:
            for attempt in range(3):
                try:
                    response = self._groq_client().chat.completions.create(
                        model=current_model,
                        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                        temperature=0.1,
                        max_tokens=max_tokens,
                    )
                    choice = response.choices[0]
                    content = (choice.message.content or "").strip()
                    if not content:
                        if hasattr(choice.message, "reasoning_content") and choice.message.reasoning_content:
                            content = str(choice.message.reasoning_content).strip()
                        elif hasattr(choice.message, "reasoning") and choice.message.reasoning:
                            content = str(choice.message.reasoning).strip()
                    if not content:
                        raise RuntimeError(f"Groq returned empty content with finish_reason={choice.finish_reason}")
                    return content
                except Exception as error:
                    last_error = error
                    err_str = str(error)
                    if "TPD" in err_str or "tokens per day" in err_str.lower():
                        logger.warning(f"Model {current_model} reached daily token limit, falling back to next available model...")
                        break
                    if ("429" in err_str or "rate_limit" in err_str.lower()) and attempt < 2:
                        sleep_time = 2.0 * (attempt + 1)
                        logger.warning(f"Groq rate limit on {current_model}; retrying in {sleep_time}s...")
                        time.sleep(sleep_time)
                    else:
                        break
        raise last_error

    def _parse_json(self, raw: str) -> Any:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"(\[.*\]|\{.*\})", cleaned, flags=re.DOTALL)
            if not match:
                raise
            return json.loads(match.group(1))

    def _workflow(self, state: TaskState) -> Workflow:
        return self.get(state["task_id"])

    def _groq_client(self) -> Groq:
        if not settings.groq_api_key:
            raise RuntimeError("GROQ_API_KEY is missing. Add it to backend/.env.")
        return Groq(api_key=settings.groq_api_key, timeout=45.0, max_retries=3)

    def _record_and_event(
        self,
        workflow: Workflow,
        event_type: str,
        actor: str,
        action: str,
        detail: str,
        step_id: int | None = None,
        verdict: str | None = None,
        level: str = "info",
        metadata: dict[str, Any] | None = None,
        worker_output: Any = None,
    ) -> None:
        event = AuditEvent(
            id=f"evt-{uuid4().hex[:7]}",
            task_id=workflow.task_id,
            step_id=step_id,
            event_type=event_type,
            actor=actor,
            action=action,
            detail=detail,
            level=level,
            metadata=metadata or {},
        )
        workflow.events.insert(0, event)
        self.audit.record_event(
            task_id=workflow.task_id,
            step_id=step_id,
            event_type=event_type,
            actor=actor,
            action=action,
            detail=detail,
            verdict=verdict,
            metadata=metadata,
            worker_output=worker_output,
        )

    def _audit_final(self, workflow: Workflow) -> None:
        self.audit.record(workflow.task_id, "synthesize_result", workflow.final_result, workflow.status, datetime.now(timezone.utc).isoformat())


engine = ManagerWorkerEngine()
