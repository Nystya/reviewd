from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

from reviewd.models import GitlabConfig, PRInfo
from reviewd.providers.base import GitProvider, parse_next_link

logger = logging.getLogger(__name__)

BOT_MARKER = '[](reviewd)'


class GitlabProvider(GitProvider):
    def __init__(self, config: GitlabConfig):
        self.client = httpx.Client(
            base_url=f'{config.url.rstrip("/")}/api/v4',
            headers={'Authorization': f'Bearer {config.token}'},
            timeout=30,
        )
        self._diff_refs: dict[tuple[str, int], dict] = {}

    def _paginate(self, url: str, params: dict | None = None) -> list[dict]:
        results = []
        while True:
            resp = self._request('GET', url, params=params)
            results.extend(resp.json())
            next_url = parse_next_link(resp.headers.get('link', ''))
            if not next_url:
                break
            # The next link carries the full query; params={} would make httpx strip it
            url = next_url
            params = None
        return results

    @staticmethod
    def _mr_url(repo_slug: str, pr_id: int | None = None) -> str:
        url = f'/projects/{quote(repo_slug, safe="")}/merge_requests'
        return url if pr_id is None else f'{url}/{pr_id}'

    def _pr_from_data(self, repo_slug: str, data: dict) -> PRInfo:
        if data.get('diff_refs'):
            self._diff_refs[(repo_slug, data['iid'])] = data['diff_refs']
        return PRInfo(
            repo_slug=repo_slug,
            pr_id=data['iid'],
            title=data['title'],
            author=data['author']['username'],
            source_branch=data['source_branch'],
            destination_branch=data['target_branch'],
            source_commit=data['sha'] or '',
            url=data['web_url'],
            draft=data.get('draft', data.get('work_in_progress', False)),
        )

    def _get_diff_refs(self, repo_slug: str, pr_id: int, source_commit: str | None) -> dict:
        refs = self._diff_refs.get((repo_slug, pr_id))
        if refs is None or (source_commit and refs['head_sha'] != source_commit):
            self.get_pr(repo_slug, pr_id)
            refs = self._diff_refs[(repo_slug, pr_id)]
        return refs

    def list_open_prs(self, repo_slug: str) -> list[PRInfo]:
        items = self._paginate(self._mr_url(repo_slug), {'state': 'opened', 'per_page': '100'})
        return [self._pr_from_data(repo_slug, item) for item in items]

    def get_pr(self, repo_slug: str, pr_id: int) -> PRInfo:
        resp = self._request('GET', self._mr_url(repo_slug, pr_id))
        return self._pr_from_data(repo_slug, resp.json())

    def post_comment(
        self,
        repo_slug: str,
        pr_id: int,
        body: str,
        *,
        file_path: str | None = None,
        line: int | None = None,
        end_line: int | None = None,
        source_commit: str | None = None,
    ) -> int:
        marked_body = f'{body}\n\n{BOT_MARKER}'
        mr_url = self._mr_url(repo_slug, pr_id)

        if file_path is None:
            resp = self._request('POST', f'{mr_url}/notes', json={'body': marked_body})
            comment_id = resp.json()['id']
        else:
            refs = self._get_diff_refs(repo_slug, pr_id, source_commit)
            position: dict = {
                'base_sha': refs['base_sha'],
                'start_sha': refs['start_sha'],
                'head_sha': refs['head_sha'],
                'old_path': file_path,
                'new_path': file_path,
            }
            target_line = end_line if end_line is not None else line
            if target_line is not None:
                position['position_type'] = 'text'
                position['new_line'] = target_line
            else:
                position['position_type'] = 'file'
            resp = self._request('POST', f'{mr_url}/discussions', json={'body': marked_body, 'position': position})
            comment_id = resp.json()['notes'][0]['id']

        logger.info('Posted comment %d on MR !%d', comment_id, pr_id)
        return comment_id

    def delete_comment(self, repo_slug: str, pr_id: int, comment_id: int) -> bool:
        resp = self._request_raw('DELETE', f'{self._mr_url(repo_slug, pr_id)}/notes/{comment_id}')
        if resp.status_code == 204:
            logger.info('Deleted comment %d on MR !%d', comment_id, pr_id)
            return True
        logger.warning('Failed to delete comment %d on MR !%d: %d', comment_id, pr_id, resp.status_code)
        return False

    def approve_pr(self, repo_slug: str, pr_id: int) -> bool:
        resp = self._request_raw('POST', f'{self._mr_url(repo_slug, pr_id)}/approve')
        if resp.status_code in (401, 403, 405):
            logger.warning('Cannot approve MR !%d (already approved or self-approve): %s', pr_id, resp.text[:200])
            return False
        resp.raise_for_status()
        logger.info('Approved MR !%d', pr_id)
        return True
