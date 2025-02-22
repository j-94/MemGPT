from abc import ABC, abstractmethod
import datetime
import re

from .utils import cosine_similarity, get_local_time, printd
from .prompts.gpt_summarize import SYSTEM as SUMMARY_PROMPT_SYSTEM
from .openai_tools import acompletions_with_backoff as acreate, async_get_embedding_with_backoff


class CoreMemory(object):
    """Held in-context inside the system message

    Core Memory: Refers to the system block, which provides essential, foundational context to the AI.
    This includes the persona information, essential user details,
    and any other baseline data you deem necessary for the AI's basic functioning.
    """

    def __init__(self, persona=None, human=None, persona_char_limit=None, human_char_limit=None, archival_memory_exists=True):
        self.persona = persona
        self.human = human
        self.persona_char_limit = persona_char_limit
        self.human_char_limit = human_char_limit

        # affects the error message the AI will see on overflow inserts
        self.archival_memory_exists = archival_memory_exists

    def __repr__(self) -> str:
        return \
            f"\n### CORE MEMORY ###" + \
            f"\n=== Persona ===\n{self.persona}" + \
            f"\n\n=== Human ===\n{self.human}"

    def to_dict(self):
        return {
            'persona': self.persona,
            'human': self.human,
        }

    @classmethod
    def load(cls, state):
        return cls(state['persona'], state['human'])

    def edit_persona(self, new_persona):
        if self.persona_char_limit and len(new_persona) > self.persona_char_limit:
            error_msg = f"Edit failed: Exceeds {self.persona_char_limit} character limit (requested {len(new_persona)})."
            if self.archival_memory_exists:
                error_msg = f"{error_msg} Consider summarizing existing core memories in 'persona' and/or moving lower priority content to archival memory to free up space in core memory, then trying again."
            raise ValueError(error_msg)

        self.persona = new_persona
        return len(self.persona)

    def edit_human(self, new_human):
        if self.human_char_limit and len(new_human) > self.human_char_limit:
            error_msg = f"Edit failed: Exceeds {self.human_char_limit} character limit (requested {len(new_human)})."
            if self.archival_memory_exists:
                error_msg = f"{error_msg} Consider summarizing existing core memories in 'human' and/or moving lower priority content to archival memory to free up space in core memory, then trying again."
            raise ValueError(error_msg)

        self.human = new_human
        return len(self.human)

    def edit(self, field, content):
        if field == 'persona':
            return self.edit_persona(content)
        elif field == 'human':
            return self.edit_human(content)
        else:
            raise KeyError

    def edit_append(self, field, content, sep='\n'):
        if field == 'persona':
            new_content = self.persona + sep + content
            return self.edit_persona(new_content)
        elif field == 'human':
            new_content = self.human + sep + content
            return self.edit_human(new_content)
        else:
            raise KeyError

    def edit_replace(self, field, old_content, new_content):
        if field == 'persona':
            if old_content in self.persona:
                new_persona = self.persona.replace(old_content, new_content)
                return self.edit_persona(new_persona)
            else:
                raise ValueError('Content not found in persona (make sure to use exact string)')
        elif field == 'human':
            if old_content in self.human:
                new_human = self.human.replace(old_content, new_content)
                return self.edit_human(new_human)
            else:
                raise ValueError('Content not found in human (make sure to use exact string)')
        else:
            raise KeyError


async def summarize_messages(
        model,
        message_sequence_to_summarize,
    ):
    """Summarize a message sequence using GPT"""

    summary_prompt = SUMMARY_PROMPT_SYSTEM
    summary_input = str(message_sequence_to_summarize)
    message_sequence = [
        {"role": "system", "content": summary_prompt},
        {"role": "user", "content": summary_input},
    ]

    response = await acreate(
        model=model,
        messages=message_sequence,
    )

    printd(f"summarize_messages gpt reply: {response.choices[0]}")
    reply = response.choices[0].message.content
    return reply


class ArchivalMemory(ABC):

    @abstractmethod
    def insert(self, memory_string):
        pass

    @abstractmethod
    def search(self, query_string, count=None, start=None):
        pass

    @abstractmethod
    def __repr__(self) -> str:
        pass


class DummyArchivalMemory(ArchivalMemory):
    """Dummy in-memory version of an archival memory database (eg run on MongoDB)

    Archival Memory: A more structured and deep storage space for the AI's reflections,
    insights, or any other data that doesn't fit into the active memory but
    is essential enough not to be left only to the recall memory.
    """

    def __init__(self, archival_memory_database=None):
        self._archive = [] if archival_memory_database is None else archival_memory_database # consists of {'content': str} dicts

    def __len__(self):
        return len(self._archive)

    def __repr__(self) -> str:
        if len(self._archive) == 0:
            memory_str = "<empty>"
        else:
            memory_str = "\n".join([d['content'] for d in self._archive])
        return \
            f"\n### ARCHIVAL MEMORY ###" + \
            f"\n{memory_str}"

    async def insert(self, memory_string, embedding=None):
        if embedding is not None:
            raise ValueError('Basic text-based archival memory does not support embeddings')
        self._archive.append({
            # can eventually upgrade to adding semantic tags, etc
            'timestamp': get_local_time(),
            'content': memory_string,
        })

    async def search(self, query_string, count=None, start=None):
        """Simple text-based search"""
        # in the dummy version, run an (inefficient) case-insensitive match search
        # printd(f"query_string: {query_string}")
        matches = [s for s in self._archive if query_string.lower() in s['content'].lower()]
        # printd(f"archive_memory.search (text-based): search for query '{query_string}' returned the following results (limit 5):\n{[str(d['content']) d in matches[:5]]}")
        printd(f"archive_memory.search (text-based): search for query '{query_string}' returned the following results (limit 5):\n{[matches[start:count]]}")

        # start/count support paging through results
        if start is not None and count is not None:
            return matches[start:start+count], len(matches)
        elif start is None and count is not None:
            return matches[:count], len(matches)
        elif start is not None and count is None:
            return matches[start:], len(matches)
        else:
            return matches, len(matches)


class DummyArchivalMemoryWithEmbeddings(DummyArchivalMemory):
    """Same as dummy in-memory archival memory, but with bare-bones embedding support"""

    def __init__(self, archival_memory_database=None, embedding_model='text-embedding-ada-002'):
        self._archive = [] if archival_memory_database is None else archival_memory_database # consists of {'content': str} dicts
        self.embedding_model = embedding_model

    def __len__(self):
        return len(self._archive)

    async def insert(self, memory_string, embedding=None):
        # Get the embedding
        if embedding is None:
            embedding = await async_get_embedding_with_backoff(memory_string, model=self.embedding_model)
        embedding_meta = {'model': self.embedding_model}
        printd(f"Got an embedding, type {type(embedding)}, len {len(embedding)}")

        self._archive.append({
            'timestamp': get_local_time(),
            'content': memory_string,
            'embedding': embedding,
            'embedding_metadata': embedding_meta,
        })

    async def search(self, query_string, count=None, start=None):
        """Simple embedding-based search (inefficient, no caching)"""
        # see: https://github.com/openai/openai-cookbook/blob/main/examples/Semantic_text_search_using_embeddings.ipynb

        # query_embedding = get_embedding(query_string, model=self.embedding_model)
        # our wrapped version supports backoff/rate-limits
        query_embedding = await async_get_embedding_with_backoff(query_string, model=self.embedding_model)
        similarity_scores = [cosine_similarity(memory['embedding'], query_embedding) for memory in self._archive]

        # Sort the archive based on similarity scores
        sorted_archive_with_scores = sorted(
            zip(self._archive, similarity_scores),
            key=lambda pair: pair[1],  # Sort by the similarity score
            reverse=True  # We want the highest similarity first
        )
        printd(f"archive_memory.search (vector-based): search for query '{query_string}' returned the following results (limit 5) and scores:\n{str([str(t[0]['content']) + '- score ' + str(t[1]) for t in sorted_archive_with_scores[:5]])}")

        # Extract the sorted archive without the scores
        matches = [item[0] for item in sorted_archive_with_scores]

        # start/count support paging through results
        if start is not None and count is not None:
            return matches[start:start+count], len(matches)
        elif start is None and count is not None:
            return matches[:count], len(matches)
        elif start is not None and count is None:
            return matches[start:], len(matches)
        else:
            return matches, len(matches)


class RecallMemory(ABC):

    @abstractmethod
    def text_search(self, query_string, count=None, start=None):
        pass

    @abstractmethod
    def date_search(self, query_string, count=None, start=None):
        pass

    @abstractmethod
    def __repr__(self) -> str:
        pass


class DummyRecallMemory(RecallMemory):
    """Dummy in-memory version of a recall memory database (eg run on MongoDB)

    Recall memory here is basically just a full conversation history with the user.
    Queryable via string matching, or date matching.

    Recall Memory: The AI's capability to search through past interactions,
    effectively allowing it to 'remember' prior engagements with a user.
    """

    def __init__(self, message_database=None, restrict_search_to_summaries=False):
        self._message_logs = [] if message_database is None else message_database  # consists of full message dicts

        # If true, the pool of messages that can be queried are the automated summaries only
        # (generated when the conversation window needs to be shortened)
        self.restrict_search_to_summaries = restrict_search_to_summaries

    def __len__(self):
        return len(self._message_logs)

    def __repr__(self) -> str:
        # don't dump all the conversations, just statistics
        system_count = user_count = assistant_count = function_count = other_count = 0
        for msg in self._message_logs:
            role = msg['message']['role']
            if role == 'system':
                system_count += 1
            elif role == 'user':
                user_count += 1
            elif role == 'assistant':
                assistant_count += 1
            elif role == 'function':
                function_count += 1
            else:
                other_count += 1
        memory_str = f"Statistics:" + \
                     f"\n{len(self._message_logs)} total messages" + \
                     f"\n{system_count} system" + \
                     f"\n{user_count} user" + \
                     f"\n{assistant_count} assistant" + \
                     f"\n{function_count} function" + \
                     f"\n{other_count} other"
        return \
            f"\n### RECALL MEMORY ###" + \
            f"\n{memory_str}"

    async def insert(self, message):
        raise NotImplementedError('This should be handled by the PersistenceManager, recall memory is just a search layer on top')

    async def text_search(self, query_string, count=None, start=None):
        # in the dummy version, run an (inefficient) case-insensitive match search
        message_pool = [d for d in self._message_logs if d['message']['role'] not in ['system', 'function']]

        printd(f"recall_memory.text_search: searching for {query_string} (c={count}, s={start}) in {len(self._message_logs)} total messages")
        matches = [d for d in message_pool if d['message']['content'] is not None and query_string.lower() in d['message']['content'].lower()]
        printd(f"recall_memory - matches:\n{matches[start:start+count]}")

        # start/count support paging through results
        if start is not None and count is not None:
            return matches[start:start+count], len(matches)
        elif start is None and count is not None:
            return matches[:count], len(matches)
        elif start is not None and count is None:
            return matches[start:], len(matches)
        else:
            return matches, len(matches)

    def _validate_date_format(self, date_str):
        """Validate the given date string in the format 'YYYY-MM-DD'."""
        try:
            datetime.datetime.strptime(date_str, '%Y-%m-%d')
            return True
        except ValueError:
            return False

    def _extract_date_from_timestamp(self, timestamp):
        """Extracts and returns the date from the given timestamp."""
        # Extracts the date (ignoring the time and timezone)
        match = re.match(r"(\d{4}-\d{2}-\d{2})", timestamp)
        return match.group(1) if match else None

    async def date_search(self, start_date, end_date, count=None, start=None):
        message_pool = [d for d in self._message_logs if d['message']['role'] not in ['system', 'function']]

        # First, validate the start_date and end_date format
        if not self._validate_date_format(start_date) or not self._validate_date_format(end_date):
            raise ValueError("Invalid date format. Expected format: YYYY-MM-DD")

        # Convert dates to datetime objects for comparison
        start_date_dt = datetime.datetime.strptime(start_date, '%Y-%m-%d')
        end_date_dt = datetime.datetime.strptime(end_date, '%Y-%m-%d')

        # Next, match items inside self._message_logs
        matches = [
            d for d in message_pool
            if start_date_dt <= datetime.datetime.strptime(self._extract_date_from_timestamp(d['timestamp']), '%Y-%m-%d') <= end_date_dt
        ]

        # start/count support paging through results
        if start is not None and count is not None:
            return matches[start:start+count], len(matches)
        elif start is None and count is not None:
            return matches[:count], len(matches)
        elif start is not None and count is None:
            return matches[start:], len(matches)
        else:
            return matches, len(matches)


class DummyRecallMemoryWithEmbeddings(DummyRecallMemory):
    """Lazily manage embeddings by keeping a string->embed dict"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.embeddings = dict()
        self.embedding_model = 'text-embedding-ada-002'
        self.only_use_preloaded_embeddings = False

    async def text_search(self, query_string, count=None, start=None):
        # in the dummy version, run an (inefficient) case-insensitive match search
        message_pool = [d for d in self._message_logs if d['message']['role'] not in ['system', 'function']]

        # first, go through and make sure we have all the embeddings we need
        message_pool_filtered = []
        for d in message_pool:
            message_str = d['message']['content']
            if self.only_use_preloaded_embeddings:
                if message_str not in self.embeddings:
                    printd(f"recall_memory.text_search -- '{message_str}' was not in embedding dict, skipping.")
                else:
                    message_pool_filtered.append(d)
            elif message_str not in self.embeddings:
                printd(f"recall_memory.text_search -- '{message_str}' was not in embedding dict, computing now")
                self.embeddings[message_str] = await async_get_embedding_with_backoff(message_str, model=self.embedding_model)
                message_pool_filtered.append(d)

       # our wrapped version supports backoff/rate-limits
        query_embedding = await async_get_embedding_with_backoff(query_string, model=self.embedding_model)
        similarity_scores = [cosine_similarity(self.embeddings[d['message']['content']], query_embedding) for d in message_pool_filtered]

        # Sort the archive based on similarity scores
        sorted_archive_with_scores = sorted(
            zip(message_pool_filtered, similarity_scores),
            key=lambda pair: pair[1],  # Sort by the similarity score
            reverse=True  # We want the highest similarity first
        )
        printd(f"recall_memory.text_search (vector-based): search for query '{query_string}' returned the following results (limit 5) and scores:\n{str([str(t[0]['message']['content']) + '- score ' + str(t[1]) for t in sorted_archive_with_scores[:5]])}")

        # Extract the sorted archive without the scores
        matches = [item[0] for item in sorted_archive_with_scores]

        # start/count support paging through results
        if start is not None and count is not None:
            return matches[start:start+count], len(matches)
        elif start is None and count is not None:
            return matches[:count], len(matches)
        elif start is not None and count is None:
            return matches[start:], len(matches)
        else:
            return matches, len(matches)
