"""
OpenAPI and Swagger UI routes for the MiroFish backend.
"""

from __future__ import annotations

import inspect
import re
import textwrap
from typing import Any

from flask import Flask, Response, jsonify, request

from .utils.logger import get_logger


logger = get_logger('mirofish.docs')

PATH_PARAMETER_PATTERN = re.compile(r'<(?:(?P<converter>[^:<>]+):)?(?P<name>[^<>]+)>')
QUERY_PARAMETER_PATTERN = re.compile(
    r"request\.args\.get\(\s*['\"](?P<name>[^'\"]+)['\"](?P<args>[^\)]*)\)",
    re.MULTILINE,
)
JSON_FIELD_PATTERN = re.compile(
    r"data\.get\(\s*['\"](?P<name>[^'\"]+)['\"](?:\s*,\s*(?P<default>[^\)]+))?\)",
    re.MULTILINE,
)
FORM_FIELD_PATTERN = re.compile(
    r"request\.form\.get\(\s*['\"](?P<name>[^'\"]+)['\"](?:\s*,\s*(?P<default>[^\)]+))?\)",
    re.MULTILINE,
)
FILE_FIELD_PATTERN = re.compile(
    r"request\.files\.(?:get|getlist)\(\s*['\"](?P<name>[^'\"]+)['\"]",
    re.MULTILINE,
)

REQUEST_BODY_OVERRIDES: dict[tuple[str, str], dict[str, Any]] = {
    ('/api/graph/ontology/generate', 'post'): {
        'requestBody': {
            'required': True,
            'content': {
                'multipart/form-data': {
                    'schema': {
                        'type': 'object',
                        'required': ['files', 'simulation_requirement'],
                        'properties': {
                            'files': {
                                'type': 'array',
                                'items': {
                                    'type': 'string',
                                    'format': 'binary',
                                },
                            },
                            'simulation_requirement': {'type': 'string'},
                            'project_name': {'type': 'string'},
                            'additional_context': {'type': 'string'},
                        },
                    }
                }
            },
        }
    }
}


def register_openapi_routes(app: Flask) -> None:
    """Register OpenAPI JSON and Swagger UI routes."""

    @app.route('/openapi.json', methods=['GET'])
    def openapi_spec() -> Response:
        logger.debug('Serving generated OpenAPI document')
        return jsonify(build_openapi_spec(app))

    @app.route('/docs', methods=['GET'])
    @app.route('/swagger', methods=['GET'])
    def swagger_ui() -> Response:
        logger.debug('Serving Swagger UI page')
        html = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>MiroFish API Docs</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css" />
    <style>
      body {
        margin: 0;
        background: linear-gradient(180deg, #f4f7f5 0%, #eef3ef 100%);
        font-family: "Segoe UI", sans-serif;
      }
      .page-header {
        padding: 24px 32px 12px;
        color: #17311f;
      }
      .page-header h1 {
        margin: 0 0 6px;
        font-size: 28px;
      }
      .page-header p {
        margin: 0;
        color: #466053;
      }
      #swagger-ui {
        margin: 0 16px 24px;
        border-radius: 18px;
        overflow: hidden;
        box-shadow: 0 18px 48px rgba(23, 49, 31, 0.08);
      }
    </style>
  </head>
  <body>
    <div class="page-header">
      <h1>MiroFish Backend API</h1>
      <p>Generated from the live Flask route table, docstrings, and request parsing patterns.</p>
    </div>
    <div id="swagger-ui"></div>
    <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
    <script>
      window.ui = SwaggerUIBundle({
        url: '/openapi.json',
        dom_id: '#swagger-ui',
        deepLinking: true,
        displayRequestDuration: true,
        docExpansion: 'list',
        defaultModelsExpandDepth: 2,
        persistAuthorization: true,
      });
    </script>
  </body>
</html>
"""
        return Response(html, mimetype='text/html')


def build_openapi_spec(app: Flask) -> dict[str, Any]:
    """Build an OpenAPI document from the registered Flask routes."""
    paths: dict[str, Any] = {}

    for rule in sorted(app.url_map.iter_rules(), key=lambda item: item.rule):
        if rule.endpoint == 'static' or rule.rule in {'/docs', '/swagger', '/openapi.json'}:
            continue

        openapi_path = _rule_to_openapi_path(rule.rule)
        path_item = paths.setdefault(openapi_path, {})
        view_func = app.view_functions.get(rule.endpoint)

        for method in sorted(rule.methods - {'HEAD', 'OPTIONS'}):
            operation = _build_operation(app, rule, method.lower(), view_func)
            path_item[method.lower()] = operation

    server_url = request.url_root.rstrip('/') if request else ''

    return {
        'openapi': '3.0.3',
        'info': {
            'title': 'MiroFish Backend API',
            'version': '1.0.0',
            'description': (
                'Interactive API reference generated from the live Flask application. '
                'Route descriptions come from endpoint docstrings, and request fields are '
                'inferred from the existing request parsing code where possible.'
            ),
        },
        'servers': [{'url': server_url}] if server_url else [],
        'tags': [
            {'name': 'System', 'description': 'Operational and health endpoints.'},
            {'name': 'Graph', 'description': 'Project, ontology, task, and graph data endpoints.'},
            {'name': 'Simulation', 'description': 'Simulation preparation, runtime, and interview endpoints.'},
            {'name': 'Report', 'description': 'Simulation report generation, retrieval, and tooling endpoints.'},
        ],
        'paths': paths,
        'components': {
            'schemas': {
                'ApiSuccess': {
                    'type': 'object',
                    'properties': {
                        'success': {'type': 'boolean', 'example': True},
                        'data': {'type': 'object', 'additionalProperties': True},
                        'message': {'type': 'string'},
                        'count': {'type': 'integer'},
                    },
                },
                'ApiError': {
                    'type': 'object',
                    'properties': {
                        'success': {'type': 'boolean', 'example': False},
                        'error': {'type': 'string'},
                        'traceback': {'type': 'string'},
                    },
                },
            }
        },
    }


def _build_operation(app: Flask, rule: Any, method: str, view_func: Any) -> dict[str, Any]:
    docstring = inspect.getdoc(view_func) if view_func else None
    source = _get_source(view_func)
    summary, description = _describe_operation(docstring, rule.rule, method)

    operation: dict[str, Any] = {
        'tags': [_infer_tag(rule.rule)],
        'operationId': f"{rule.endpoint.replace('.', '_')}_{method}",
        'summary': summary,
        'description': description,
        'parameters': _build_parameters(rule, source),
        'responses': {
            '200': {
                'description': 'Successful response',
                'content': {
                    'application/json': {
                        'schema': {'$ref': '#/components/schemas/ApiSuccess'}
                    }
                },
            },
            '400': {
                'description': 'Client-side validation error',
                'content': {
                    'application/json': {
                        'schema': {'$ref': '#/components/schemas/ApiError'}
                    }
                },
            },
            '404': {
                'description': 'Requested resource was not found',
                'content': {
                    'application/json': {
                        'schema': {'$ref': '#/components/schemas/ApiError'}
                    }
                },
            },
            '500': {
                'description': 'Unhandled server error',
                'content': {
                    'application/json': {
                        'schema': {'$ref': '#/components/schemas/ApiError'}
                    }
                },
            },
        },
    }

    override = REQUEST_BODY_OVERRIDES.get((_rule_to_openapi_path(rule.rule), method))
    if override:
        operation.update(override)
    else:
        request_body = _build_request_body(method, source)
        if request_body:
            operation['requestBody'] = request_body

    if not operation['parameters']:
        operation.pop('parameters')

    return operation


def _build_parameters(rule: Any, source: str) -> list[dict[str, Any]]:
    parameters: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for match in PATH_PARAMETER_PATTERN.finditer(rule.rule):
        name = match.group('name')
        converter = match.group('converter') or 'string'
        parameters.append({
            'name': name,
            'in': 'path',
            'required': True,
            'schema': _schema_for_converter(converter),
        })
        seen.add(('path', name))

    for match in QUERY_PARAMETER_PATTERN.finditer(source):
        name = match.group('name')
        signature = match.group('args') or ''
        key = ('query', name)
        if key in seen:
            continue
        parameters.append({
            'name': name,
            'in': 'query',
            'required': False,
            'schema': _schema_from_signature(name, signature),
        })
        seen.add(key)

    return parameters


def _build_request_body(method: str, source: str) -> dict[str, Any] | None:
    if method not in {'post', 'put', 'patch'}:
        return None

    json_properties = _extract_json_fields(source)
    form_properties = _extract_form_fields(source)
    file_fields = sorted({match.group('name') for match in FILE_FIELD_PATTERN.finditer(source)})

    if file_fields or form_properties:
        properties = {**form_properties}
        for name in file_fields:
            properties[name] = {
                'type': 'array' if 'getlist' in source else 'string',
                'items': {'type': 'string', 'format': 'binary'} if 'getlist' in source else None,
                'format': None if 'getlist' in source else 'binary',
            }
            if properties[name]['items'] is None:
                properties[name].pop('items')
        return {
            'required': False,
            'content': {
                'multipart/form-data': {
                    'schema': {
                        'type': 'object',
                        'properties': properties,
                    }
                }
            },
        }

    if json_properties:
        return {
            'required': False,
            'content': {
                'application/json': {
                    'schema': {
                        'type': 'object',
                        'properties': json_properties,
                        'additionalProperties': True,
                    }
                }
            },
        }

    return {
        'required': False,
        'content': {
            'application/json': {
                'schema': {
                    'type': 'object',
                    'additionalProperties': True,
                }
            }
        },
    }


def _extract_json_fields(source: str) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for match in JSON_FIELD_PATTERN.finditer(source):
        name = match.group('name')
        default_value = (match.group('default') or '').strip()
        properties[name] = _schema_from_default(name, default_value)
    return properties


def _extract_form_fields(source: str) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for match in FORM_FIELD_PATTERN.finditer(source):
        name = match.group('name')
        default_value = (match.group('default') or '').strip()
        properties[name] = _schema_from_default(name, default_value)
    return properties


def _describe_operation(docstring: str | None, route: str, method: str) -> tuple[str, str]:
    if not docstring:
        summary = f'{method.upper()} {route}'
        return summary, 'Auto-generated operation documentation.'

    cleaned = textwrap.dedent(docstring).strip()
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    summary = lines[0] if lines else f'{method.upper()} {route}'
    description = cleaned
    return summary, description


def _infer_tag(route: str) -> str:
    if route.startswith('/api/graph'):
        return 'Graph'
    if route.startswith('/api/simulation'):
        return 'Simulation'
    if route.startswith('/api/report'):
        return 'Report'
    return 'System'


def _schema_for_converter(converter: str) -> dict[str, Any]:
    if converter == 'int':
        return {'type': 'integer'}
    if converter == 'float':
        return {'type': 'number', 'format': 'float'}
    return {'type': 'string'}


def _schema_from_signature(name: str, signature: str) -> dict[str, Any]:
    if 'type=int' in signature:
        return {'type': 'integer'}
    if 'type=float' in signature:
        return {'type': 'number', 'format': 'float'}
    if 'type=bool' in signature:
        return {'type': 'boolean'}
    return _schema_from_default(name, signature)


def _schema_from_default(name: str, default_value: str) -> dict[str, Any]:
    cleaned = default_value.strip()
    if cleaned in {'True', 'False'}:
        return {'type': 'boolean'}
    if cleaned.isdigit() or name in {'limit', 'offset', 'timeout', 'agent_id', 'section_index'}:
        return {'type': 'integer'}
    if name.startswith('enable_') or name.startswith('force_') or name == 'force':
        return {'type': 'boolean'}
    if name == 'interviews':
        return {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': True,
            },
        }
    if name == 'chat_history':
        return {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': True,
            },
        }
    return {'type': 'string'}


def _rule_to_openapi_path(route: str) -> str:
    return PATH_PARAMETER_PATTERN.sub(lambda match: '{' + match.group('name') + '}', route)


def _get_source(view_func: Any) -> str:
    if not view_func:
        return ''
    try:
        return inspect.getsource(view_func)
    except (OSError, TypeError):
        logger.debug('Could not inspect source for %s', getattr(view_func, '__name__', 'unknown'))
        return ''