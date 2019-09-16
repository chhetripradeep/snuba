import itertools

from collections import ChainMap
from dataclasses import dataclass

from typing import Any, Mapping

from snuba.schemas import (
    validate_jsonschema,
    Schema
)


@dataclass(frozen=True)
class Request:
    query: Mapping[str, Any]
    extensions: Mapping[str, Mapping[str, Any]]

    @property
    def body(self):
        return ChainMap(self.query, *self.extensions.values())


class RequestSchema:
    def __init__(self, query_schema: Schema, extensions_schemas: Mapping[str, Schema]):
        self.__query_schema = query_schema
        self.__extension_schemas = extensions_schemas

        self.__composite_schema = {
            'type': 'object',
            'properties': {},
            'required': [],
            'definitions': {},
            'additionalProperties': False,
        }

        for schema in itertools.chain([self.__query_schema], self.__extension_schemas.values()):
            assert schema['type'] == 'object', 'subschema must be object'
            assert schema['additionalProperties'] is False, 'subschema must not allow additional properties'
            self.__composite_schema['required'].extend(schema.get('required', []))

            for property_name, property_schema in schema['properties'].items():
                assert property_name not in self.__composite_schema['properties'], 'subschema cannot redefine property'
                self.__composite_schema['properties'][property_name] = property_schema

            for definition_name, definition_schema in schema.get('definitions', {}).items():
                assert definition_name not in self.__composite_schema['definitions'], 'subschema cannot redefine definition'
                self.__composite_schema['definitions'][definition_name] = definition_schema

        self.__composite_schema['required'] = set(self.__composite_schema['required'])

    def validate(self, value) -> Request:
        value = validate_jsonschema(value, self.__composite_schema)

        query = {key: value.pop(key) for key in self.__query_schema['properties'].keys() if key in value}

        extensions = {}
        for extension_name, extension_schema in self.__extension_schemas.items():
            extensions[extension_name] = {key: value.pop(key) for key in extension_schema['properties'].keys() if key in value}

        return Request(query, extensions)

    def __generate_template_impl(self, schema) -> Any:
        """
        Generate a (not necessarily valid) object that can be used as a template
        from the provided schema
        """
        typ = schema.get('type')
        if 'default' in schema:
            default = schema['default']
            return default() if callable(default) else default
        elif typ == 'object':
            return {prop: self.__generate_template_impl(subschema) for prop, subschema in schema.get('properties', {}).items()}
        elif typ == 'array':
            return []
        elif typ == 'string':
            return ""
        return None

    def generate_template(self) -> Any:
        return self.__generate_template_impl(self.__composite_schema)