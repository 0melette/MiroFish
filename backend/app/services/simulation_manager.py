"""
OASIS simulation manager.
Manages parallel Twitter and Reddit simulations.
Uses preset scripts plus LLM-generated configuration parameters.
"""

import os
import json
import shutil
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from ..config import Config
from ..utils.logger import get_logger
from .zep_entity_reader import ZepEntityReader, FilteredEntities, EntityNode
from .oasis_profile_generator import OasisProfileGenerator, OasisAgentProfile
from .simulation_config_generator import SimulationConfigGenerator, SimulationParameters

logger = get_logger('mirofish.simulation')


class SimulationStatus(str, Enum):
    """Simulation status."""
    CREATED = "created"
    PREPARING = "preparing"
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"      # The simulation was stopped manually.
    COMPLETED = "completed"  # The simulation completed naturally.
    FAILED = "failed"


class PlatformType(str, Enum):
    """Platform type."""
    TWITTER = "twitter"
    REDDIT = "reddit"


@dataclass
class SimulationState:
    """Simulation state."""
    simulation_id: str
    project_id: str
    graph_id: str
    
    # Platform enablement flags.
    enable_twitter: bool = True
    enable_reddit: bool = True
    
    # Status.
    status: SimulationStatus = SimulationStatus.CREATED
    
    # Preparation phase data.
    entities_count: int = 0
    profiles_count: int = 0
    entity_types: List[str] = field(default_factory=list)
    
    # Configuration generation metadata.
    config_generated: bool = False
    config_reasoning: str = ""
    
    # Runtime data.
    current_round: int = 0
    twitter_status: str = "not_started"
    reddit_status: str = "not_started"
    
    # Timestamps.
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    
    # Error information.
    error: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Full state dictionary for internal use."""
        return {
            "simulation_id": self.simulation_id,
            "project_id": self.project_id,
            "graph_id": self.graph_id,
            "enable_twitter": self.enable_twitter,
            "enable_reddit": self.enable_reddit,
            "status": self.status.value,
            "entities_count": self.entities_count,
            "profiles_count": self.profiles_count,
            "entity_types": self.entity_types,
            "config_generated": self.config_generated,
            "config_reasoning": self.config_reasoning,
            "current_round": self.current_round,
            "twitter_status": self.twitter_status,
            "reddit_status": self.reddit_status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
        }
    
    def to_simple_dict(self) -> Dict[str, Any]:
        """Simplified state dictionary for API responses."""
        return {
            "simulation_id": self.simulation_id,
            "project_id": self.project_id,
            "graph_id": self.graph_id,
            "status": self.status.value,
            "entities_count": self.entities_count,
            "profiles_count": self.profiles_count,
            "entity_types": self.entity_types,
            "config_generated": self.config_generated,
            "error": self.error,
        }


class SimulationManager:
    """
    Simulation manager.

    Core responsibilities:
    1. Read and filter entities from the Zep graph.
    2. Generate OASIS agent profiles.
    3. Use the LLM to generate simulation configuration parameters.
    4. Prepare all files required by the preset simulation scripts.
    """
    
    # Simulation data storage directory.
    SIMULATION_DATA_DIR = os.path.join(
        os.path.dirname(__file__), 
        '../../uploads/simulations'
    )
    
    def __init__(self):
        # Ensure the storage directory exists.
        os.makedirs(self.SIMULATION_DATA_DIR, exist_ok=True)
        
        # In-memory simulation state cache.
        self._simulations: Dict[str, SimulationState] = {}
    
    def _get_simulation_dir(self, simulation_id: str) -> str:
        """Get the simulation data directory."""
        sim_dir = os.path.join(self.SIMULATION_DATA_DIR, simulation_id)
        os.makedirs(sim_dir, exist_ok=True)
        return sim_dir
    
    def _save_simulation_state(self, state: SimulationState):
        """Persist simulation state to disk."""
        sim_dir = self._get_simulation_dir(state.simulation_id)
        state_file = os.path.join(sim_dir, "state.json")
        
        state.updated_at = datetime.now().isoformat()
        
        with open(state_file, 'w', encoding='utf-8') as f:
            json.dump(state.to_dict(), f, ensure_ascii=False, indent=2)
        
        self._simulations[state.simulation_id] = state
    
    def _load_simulation_state(self, simulation_id: str) -> Optional[SimulationState]:
        """Load simulation state from disk."""
        if simulation_id in self._simulations:
            return self._simulations[simulation_id]
        
        sim_dir = self._get_simulation_dir(simulation_id)
        state_file = os.path.join(sim_dir, "state.json")
        
        if not os.path.exists(state_file):
            return None
        
        with open(state_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        state = SimulationState(
            simulation_id=simulation_id,
            project_id=data.get("project_id", ""),
            graph_id=data.get("graph_id", ""),
            enable_twitter=data.get("enable_twitter", True),
            enable_reddit=data.get("enable_reddit", True),
            status=SimulationStatus(data.get("status", "created")),
            entities_count=data.get("entities_count", 0),
            profiles_count=data.get("profiles_count", 0),
            entity_types=data.get("entity_types", []),
            config_generated=data.get("config_generated", False),
            config_reasoning=data.get("config_reasoning", ""),
            current_round=data.get("current_round", 0),
            twitter_status=data.get("twitter_status", "not_started"),
            reddit_status=data.get("reddit_status", "not_started"),
            created_at=data.get("created_at", datetime.now().isoformat()),
            updated_at=data.get("updated_at", datetime.now().isoformat()),
            error=data.get("error"),
        )
        
        self._simulations[simulation_id] = state
        return state
    
    def create_simulation(
        self,
        project_id: str,
        graph_id: str,
        enable_twitter: bool = True,
        enable_reddit: bool = True,
    ) -> SimulationState:
        """
        Create a new simulation.
        
        Args:
            project_id: Project ID.
            graph_id: Zep graph ID.
            enable_twitter: Whether to enable Twitter simulation.
            enable_reddit: Whether to enable Reddit simulation.
            
        Returns:
            SimulationState
        """
        import uuid
        simulation_id = f"sim_{uuid.uuid4().hex[:12]}"
        
        state = SimulationState(
            simulation_id=simulation_id,
            project_id=project_id,
            graph_id=graph_id,
            enable_twitter=enable_twitter,
            enable_reddit=enable_reddit,
            status=SimulationStatus.CREATED,
        )
        
        self._save_simulation_state(state)
        logger.info(f"Created simulation: {simulation_id}, project={project_id}, graph={graph_id}")
        
        return state

    def _resolve_prepare_entities(
        self,
        entities: List[EntityNode],
        selected_entity_uuids: Optional[List[str]] = None,
        max_agents: Optional[int] = None
    ) -> List[EntityNode]:
        """Resolve the final entity set that will participate in the simulation."""
        resolved_entities = list(entities)

        if selected_entity_uuids:
            entity_map = {entity.uuid: entity for entity in entities}
            resolved_entities = [
                entity_map[entity_uuid]
                for entity_uuid in selected_entity_uuids
                if entity_uuid in entity_map
            ]

        if max_agents is not None:
            resolved_entities = resolved_entities[:max_agents]

        return resolved_entities

    def _normalize_override_topics(self, topics: Any) -> List[str]:
        """Normalize user-provided topic override values."""
        if isinstance(topics, list):
            return [str(topic).strip() for topic in topics if str(topic).strip()]
        if isinstance(topics, str):
            return [topic.strip() for topic in topics.split(',') if topic.strip()]
        return []

    def _apply_override_to_entity(self, entity: EntityNode, override: Dict[str, Any]):
        """Inject user edits into the entity before config and profile generation."""
        if not override:
            return

        custom_name = override.get("name")
        if custom_name:
            entity.name = str(custom_name).strip()

        summary_parts = []
        for field in ["bio", "persona"]:
            value = override.get(field)
            if value:
                summary_parts.append(str(value).strip())

        profession = override.get("profession")
        if profession:
            entity.attributes["profession"] = str(profession).strip()

        country = override.get("country")
        if country:
            entity.attributes["country"] = str(country).strip()

        mbti = override.get("mbti")
        if mbti:
            entity.attributes["mbti"] = str(mbti).strip()

        age = override.get("age")
        if age not in [None, ""]:
            entity.attributes["age"] = age

        gender = override.get("gender")
        if gender:
            entity.attributes["gender"] = str(gender).strip()

        interested_topics = self._normalize_override_topics(override.get("interested_topics"))
        if interested_topics:
            entity.attributes["interested_topics"] = interested_topics
            summary_parts.append(f"Topics: {', '.join(interested_topics)}")

        if summary_parts:
            entity.summary = " ".join(summary_parts)

    def _apply_override_to_profile(self, profile: OasisAgentProfile, override: Dict[str, Any]):
        """Apply user-edited fields to the generated profile."""
        if not override:
            return

        simple_string_fields = {
            "username": "user_name",
            "name": "name",
            "bio": "bio",
            "persona": "persona",
            "country": "country",
            "profession": "profession",
            "mbti": "mbti",
            "gender": "gender",
        }

        for source_key, target_attr in simple_string_fields.items():
            value = override.get(source_key)
            if value not in [None, ""]:
                setattr(profile, target_attr, str(value).strip())

        age = override.get("age")
        if age not in [None, ""]:
            try:
                profile.age = int(age)
            except (TypeError, ValueError):
                logger.warning(f"Ignoring invalid age override: {age}")

        interested_topics = self._normalize_override_topics(override.get("interested_topics"))
        if interested_topics:
            profile.interested_topics = interested_topics
    
    def prepare_simulation(
        self,
        simulation_id: str,
        simulation_requirement: str,
        document_text: str,
        defined_entity_types: Optional[List[str]] = None,
        use_llm_for_profiles: bool = True,
        progress_callback: Optional[callable] = None,
        parallel_profile_count: int = 3,
        max_agents: Optional[int] = None,
        selected_entity_uuids: Optional[List[str]] = None,
        profile_overrides: Optional[Dict[str, Dict[str, Any]]] = None
    ) -> SimulationState:
        """
        Prepare the simulation environment end to end.
        
        Steps:
        1. Read and filter entities from the Zep graph.
        2. Generate an OASIS Agent profile for each entity, optionally with LLM enrichment and parallelism.
        3. Use the LLM to generate simulation parameters such as time, activity, and posting frequency.
        4. Save configuration and profile files.
        5. Copy preset scripts into the simulation directory.
        
        Args:
            simulation_id: Simulation ID.
            simulation_requirement: Simulation requirement description used for LLM config generation.
            document_text: Source document content used for context.
            defined_entity_types: Predefined entity types, if any.
            use_llm_for_profiles: Whether to use the LLM to generate detailed personas.
            progress_callback: Progress callback in the form (stage, progress, message).
            parallel_profile_count: Number of profiles to generate in parallel. Defaults to 3.
            max_agents: Maximum number of entities to include in the simulation.
            selected_entity_uuids: Exact entity UUIDs to include.
            profile_overrides: User-defined persona overrides.
            
        Returns:
            SimulationState
        """
        state = self._load_simulation_state(simulation_id)
        if not state:
            raise ValueError(f"Simulation does not exist: {simulation_id}")
        
        try:
            state.status = SimulationStatus.PREPARING
            self._save_simulation_state(state)
            
            sim_dir = self._get_simulation_dir(simulation_id)
            
            # ========== Stage 1: Read and filter entities ==========
            if progress_callback:
                progress_callback("reading", 0, "Connecting to the Zep graph...")
            
            reader = ZepEntityReader()
            
            if progress_callback:
                progress_callback("reading", 30, "Loading node data...")
            
            filtered = reader.filter_defined_entities(
                graph_id=state.graph_id,
                defined_entity_types=defined_entity_types,
                enrich_with_edges=True
            )

            original_filtered_count = filtered.filtered_count

            resolved_entities = self._resolve_prepare_entities(
                filtered.entities,
                selected_entity_uuids=selected_entity_uuids,
                max_agents=max_agents
            )

            filtered.entities = resolved_entities
            filtered.filtered_count = len(resolved_entities)
            filtered.entity_types = {
                entity.get_entity_type() or "Entity"
                for entity in resolved_entities
            }

            logger.info(
                "Simulation entity selection completed: simulation_id=%s, original_entities=%s, selected_entities=%s, max_agents=%s, final_entities=%s",
                simulation_id,
                original_filtered_count,
                len(selected_entity_uuids or []),
                max_agents,
                filtered.filtered_count,
            )

            profile_overrides = profile_overrides or {}
            for entity in filtered.entities:
                self._apply_override_to_entity(entity, profile_overrides.get(entity.uuid, {}))
            
            state.entities_count = filtered.filtered_count
            state.entity_types = list(filtered.entity_types)
            
            if progress_callback:
                progress_callback(
                    "reading", 100, 
                    f"Completed. Found {filtered.filtered_count} entities.",
                    current=filtered.filtered_count,
                    total=filtered.filtered_count
                )
            
            if filtered.filtered_count == 0:
                state.status = SimulationStatus.FAILED
                state.error = "No matching entities were found. Check whether the graph was built correctly."
                self._save_simulation_state(state)
                return state
            
            # ========== Stage 2: Generate Agent profiles ==========
            total_entities = len(filtered.entities)
            
            if progress_callback:
                progress_callback(
                    "generating_profiles", 0, 
                    "Starting generation...",
                    current=0,
                    total=total_entities
                )
            
            # Pass graph_id to enable Zep retrieval for richer context.
            generator = OasisProfileGenerator(graph_id=state.graph_id)
            
            def profile_progress(current, total, msg):
                if progress_callback:
                    progress_callback(
                        "generating_profiles", 
                        int(current / total * 100), 
                        msg,
                        current=current,
                        total=total,
                        item_name=msg
                    )
            
            # Set the realtime output path, preferring Reddit JSON format.
            realtime_output_path = None
            realtime_platform = "reddit"
            if state.enable_reddit:
                realtime_output_path = os.path.join(sim_dir, "reddit_profiles.json")
                realtime_platform = "reddit"
            elif state.enable_twitter:
                realtime_output_path = os.path.join(sim_dir, "twitter_profiles.csv")
                realtime_platform = "twitter"
            
            profiles = generator.generate_profiles_from_entities(
                entities=filtered.entities,
                use_llm=use_llm_for_profiles,
                progress_callback=profile_progress,
                graph_id=state.graph_id,  # Provide graph_id for Zep retrieval.
                parallel_count=parallel_profile_count,  # Parallel generation count.
                realtime_output_path=realtime_output_path,  # Realtime output path.
                output_platform=realtime_platform  # Output format.
            )

            for profile in profiles:
                if not profile:
                    continue
                self._apply_override_to_profile(
                    profile,
                    profile_overrides.get(profile.source_entity_uuid or "", {})
                )
            
            state.profiles_count = len(profiles)
            
            # Save profile files. Twitter uses CSV and Reddit uses JSON.
            # Reddit was already written during generation, but save again to ensure completeness.
            if progress_callback:
                progress_callback(
                    "generating_profiles", 95, 
                    "Saving profile files...",
                    current=total_entities,
                    total=total_entities
                )
            
            if state.enable_reddit:
                generator.save_profiles(
                    profiles=profiles,
                    file_path=os.path.join(sim_dir, "reddit_profiles.json"),
                    platform="reddit"
                )
            
            if state.enable_twitter:
                # Twitter must use CSV format for OASIS.
                generator.save_profiles(
                    profiles=profiles,
                    file_path=os.path.join(sim_dir, "twitter_profiles.csv"),
                    platform="twitter"
                )
            
            if progress_callback:
                progress_callback(
                    "generating_profiles", 100, 
                    f"完成，共 {len(profiles)} 个Profile",
                    current=len(profiles),
                    total=len(profiles)
                )
            
            # ========== 阶段3: LLM智能生成模拟配置 ==========
            if progress_callback:
                progress_callback(
                    "generating_config", 0, 
                    "正在分析模拟需求...",
                    current=0,
                    total=3
                )
            
            config_generator = SimulationConfigGenerator()
            
            if progress_callback:
                progress_callback(
                    "generating_config", 30, 
                    "正在调用LLM生成配置...",
                    current=1,
                    total=3
                )
            
            sim_params = config_generator.generate_config(
                simulation_id=simulation_id,
                project_id=state.project_id,
                graph_id=state.graph_id,
                simulation_requirement=simulation_requirement,
                document_text=document_text,
                entities=filtered.entities,
                enable_twitter=state.enable_twitter,
                enable_reddit=state.enable_reddit
            )
            
            if progress_callback:
                progress_callback(
                    "generating_config", 70, 
                    "正在保存配置文件...",
                    current=2,
                    total=3
                )
            
            # 保存配置文件
            config_path = os.path.join(sim_dir, "simulation_config.json")
            with open(config_path, 'w', encoding='utf-8') as f:
                f.write(sim_params.to_json())
            
            state.config_generated = True
            state.config_reasoning = sim_params.generation_reasoning
            
            if progress_callback:
                progress_callback(
                    "generating_config", 100, 
                    "配置生成完成",
                    current=3,
                    total=3
                )
            
            # 注意：运行脚本保留在 backend/scripts/ 目录，不再复制到模拟目录
            # 启动模拟时，simulation_runner 会从 scripts/ 目录运行脚本
            
            # 更新状态
            state.status = SimulationStatus.READY
            self._save_simulation_state(state)
            
            logger.info(f"模拟准备完成: {simulation_id}, "
                       f"entities={state.entities_count}, profiles={state.profiles_count}")
            
            return state
            
        except Exception as e:
            logger.error(f"模拟准备失败: {simulation_id}, error={str(e)}")
            import traceback
            logger.error(traceback.format_exc())
            state.status = SimulationStatus.FAILED
            state.error = str(e)
            self._save_simulation_state(state)
            raise
    
    def get_simulation(self, simulation_id: str) -> Optional[SimulationState]:
        """获取模拟状态"""
        return self._load_simulation_state(simulation_id)
    
    def list_simulations(self, project_id: Optional[str] = None) -> List[SimulationState]:
        """列出所有模拟"""
        simulations = []
        
        if os.path.exists(self.SIMULATION_DATA_DIR):
            for sim_id in os.listdir(self.SIMULATION_DATA_DIR):
                # 跳过隐藏文件（如 .DS_Store）和非目录文件
                sim_path = os.path.join(self.SIMULATION_DATA_DIR, sim_id)
                if sim_id.startswith('.') or not os.path.isdir(sim_path):
                    continue
                
                state = self._load_simulation_state(sim_id)
                if state:
                    if project_id is None or state.project_id == project_id:
                        simulations.append(state)
        
        return simulations
    
    def get_profiles(self, simulation_id: str, platform: str = "reddit") -> List[Dict[str, Any]]:
        """获取模拟的Agent Profile"""
        state = self._load_simulation_state(simulation_id)
        if not state:
            raise ValueError(f"模拟不存在: {simulation_id}")
        
        sim_dir = self._get_simulation_dir(simulation_id)
        profile_path = os.path.join(sim_dir, f"{platform}_profiles.json")
        
        if not os.path.exists(profile_path):
            return []
        
        with open(profile_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    def get_simulation_config(self, simulation_id: str) -> Optional[Dict[str, Any]]:
        """获取模拟配置"""
        sim_dir = self._get_simulation_dir(simulation_id)
        config_path = os.path.join(sim_dir, "simulation_config.json")
        
        if not os.path.exists(config_path):
            return None
        
        with open(config_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    def get_run_instructions(self, simulation_id: str) -> Dict[str, str]:
        """获取运行说明"""
        sim_dir = self._get_simulation_dir(simulation_id)
        config_path = os.path.join(sim_dir, "simulation_config.json")
        scripts_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../scripts'))
        
        return {
            "simulation_dir": sim_dir,
            "scripts_dir": scripts_dir,
            "config_file": config_path,
            "commands": {
                "twitter": f"python {scripts_dir}/run_twitter_simulation.py --config {config_path}",
                "reddit": f"python {scripts_dir}/run_reddit_simulation.py --config {config_path}",
                "parallel": f"python {scripts_dir}/run_parallel_simulation.py --config {config_path}",
            },
            "instructions": (
                f"1. 激活conda环境: conda activate MiroFish\n"
                f"2. 运行模拟 (脚本位于 {scripts_dir}):\n"
                f"   - 单独运行Twitter: python {scripts_dir}/run_twitter_simulation.py --config {config_path}\n"
                f"   - 单独运行Reddit: python {scripts_dir}/run_reddit_simulation.py --config {config_path}\n"
                f"   - 并行运行双平台: python {scripts_dir}/run_parallel_simulation.py --config {config_path}"
            )
        }
