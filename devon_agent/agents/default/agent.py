import json
import logging
import time
from dataclasses import dataclass, field
import traceback
from typing import Optional, Tuple

from devon_agent.agents.model import AnthropicModel, ModelArguments, OpenAiModel
from devon_agent.agents.default.anthropic_prompts import anthropic_history_to_bash_history, anthropic_last_user_prompt_template_v3, anthropic_system_prompt_template_v3, anthropic_commands_to_command_docs
from devon_agent.agents.default.openai_prompts import openai_last_user_prompt_template_v3, openai_system_prompt_template_v3, openai_commands_to_command_docs
from devon_agent.agents.default.anthropic_prompts import (
    parse_response
)
from devon_agent.tools.utils import get_cwd

from devon_agent.udiff import Hallucination
from devon_agent.utils import LOGGER_NAME, DotDict
from tenacity import RetryError

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from devon_agent.session import Session


logger = logging.getLogger(LOGGER_NAME)


@dataclass(frozen=False)
class Agent:
    name: str
    model: str
    temperature: float = 0.0
    chat_history: list[dict[str, str]] = field(default_factory=list)
    interrupt: str = ""
    api_key: Optional[str] = None
    scratchpad = None

    def run(self, session: "Session", observation: str = None): ...


class TaskAgent(Agent):
    supported_models = {
        "gpt4-o": OpenAiModel,
        "claude-opus": AnthropicModel
    }

    def _format_editor_entry(self, k, v, PAGE_SIZE=50):
        path = k
        page = v["page"]
        content_lines = v["lines"].splitlines()

        all_lines_len = len(content_lines)
        last_idx = all_lines_len // PAGE_SIZE
        if page == last_idx:
            content_len = all_lines_len % PAGE_SIZE
        else:
            content_len = PAGE_SIZE

        start_idx = page * PAGE_SIZE
        lines = content_lines[start_idx : start_idx + content_len]
        window_lines = "\n".join(
            [str(i + start_idx).zfill(4) + line for i, line in enumerate(lines)]
        )

        return f"""
************ FILE: {path}, WINDOW STARTLINE: {start_idx}, WINDOW ENDLINE: {start_idx+content_len}, TOTAL FILE LINES: {all_lines_len} ************
{window_lines}
************************************
"""

    def _convert_editor_to_view(self, editor, PAGE_SIZE=50):
        return "\n".join(
            [self._format_editor_entry(k, v, PAGE_SIZE) for k, v in editor.items()]
        )

    def predict(
        self,
        task: str,
        observation: str,
        session: "Session",
    ) -> Tuple[str, str, str]:

        if self.model not in self.supported_models:
            raise Exception("Model not supported")

        self.current_model = self.supported_models[self.model](
            args=ModelArguments(
                model_name=self.model,
                temperature=self.temperature,
                api_key=self.api_key,
            )
        )

        if self.interrupt:
            observation = observation + ". also " + self.interrupt
            self.interrupt = ""

        try:
            editor = self._convert_editor_to_view(
                session.state.editor.files, session.state.editor.PAGE_SIZE
            )

            self.chat_history.append(
                {"role": "user", "content": observation, "agent": self.name}
            )


            command_docs = list(session.generate_command_docs().values())

            output = ""

            last_observation = None
            second_last_observation = None
            if len(self.chat_history) > 2:
                last_observation = self.chat_history[-1]["content"]
                second_last_observation = self.chat_history[-3]["content"]
            if (
                last_observation
                and second_last_observation
                and "Failed to edit file" in last_observation
                and "Failed to edit file" in second_last_observation
            ):
                self.chat_history = self.chat_history[:-6]
                self.current_model.args.temperature += (
                    0.2 if self.current_model.args.temperature < 0.8 else 0
                )
            
            if self.model == "claude-opus":

                command_docs = (
                    "Custom Commands Documentation:\n"
                    + anthropic_commands_to_command_docs(
                        command_docs
                    )
                    + "\n"
                )

                history = anthropic_history_to_bash_history(self.chat_history)
                system_prompt = anthropic_system_prompt_template_v3(command_docs)
                last_user_prompt = anthropic_last_user_prompt_template_v3(
                    task, history, editor, get_cwd(
                        {
                            "session" : session,
                            "environment" : session.default_environment,
                            "state" : session.state
                        }
                    ), session.base_path, self.scratchpad
                )

                messages = [{"role": "user", "content": last_user_prompt}]
            elif self.model == "gpt4-o":
                
                time.sleep(3)

                command_docs = (
                    "Custom Commands Documentation:\n"
                    + openai_commands_to_command_docs(
                        command_docs
                    )
                    + "\n"
                )

                history = [entry for entry in self.chat_history if entry["role"] == "user" or entry["role"] == "assistant"]
                system_prompt = openai_system_prompt_template_v3(command_docs)
                last_user_prompt = openai_last_user_prompt_template_v3(
                    task,
                    editor,
                    get_cwd(
                        {
                            "session" : session,
                            "environment" : session.default_environment,
                            "state" : session.state
                        }
                    ),
                    session.base_path,
                    self.scratchpad
                )

                messages = history + [{"role": "user", "content": last_user_prompt}]

            output = self.current_model.query(messages, system_message=system_prompt)

            thought = None
            action = None

            try:
                thought, action, scratchpad = parse_response(output)
                if scratchpad:
                    self.scratchpad = scratchpad
            except Exception:
                raise Hallucination(f"Multiple actions found in response: {output}")

            if not thought or not action:
                raise Hallucination("Agent failed to follow response format instructions")

            self.chat_history.append(
                {
                    "role": "assistant",
                    "content": output,
                    "thought": thought,
                    "action": action,
                    "agent": self.name,
                }
            )

            logger.info(f"""
\n\n\n\n****************\n\n
NAME: {self.name}                        

THOUGHT: {thought}

ACTION: {action}

OBSERVATION: {observation}

SCRATCHPAD: {scratchpad}
\n\n****************\n\n\n\n""")

            return thought, action, output
        except KeyboardInterrupt:
            raise
        except Hallucination as e:
            return "hallucination","hallucination","Incorrect response format"
        except RuntimeError as e:
            session.event_log.append(
                {
                    "type": "Error",
                    "content": str(e),
                    "producer": self.name,
                    "consumer": "none",
                }
            )
            logger.error(f"Runtime error: {e}")
            return (
                f"Exit due to runtime error: {e}",
                "exit_error",
                f"exit due to runtime error: {e}",
            )
        except RetryError as e:
            session.event_log.append(
                {
                    "type": "Error",
                    "content": str(e),
                    "producer": self.name,
                    "consumer": "none",
                }
            )
            logger.error(f"Retry error: {e}")
            return (
                f"Exit due to retry error: {e}",
                "exit_api",
                f"exit due to retry error: {e}",
            )
        except Exception as e:
            session.event_log.append(
                {
                    "type": "Error",
                    "content": str(e),
                    "producer": self.name,
                    "consumer": "none",
                }
            )
            traceback.print_exc()
            logger.error(f"Exception: {e}")
            return (
                f"Exit due to exception: {e}",
                "exit_error",
                f"exit due to exception: {e}",
            )
