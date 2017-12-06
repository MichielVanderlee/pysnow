# -*- coding: utf-8 -*-

import ijson

from ijson.common import ObjectBuilder

from itertools import chain

from requests.exceptions import HTTPError

from .exceptions import (ResponseError,
                         NoResults,
                         MultipleResults,
                         MissingResult)


class Response(object):
    """Takes a :class:`requests.Response` object and performs deserialization and validation.

    :param response: :class:`request.Response` object
    :param raise_on_empty: whether or not to raise an exception if the content doesn't contain any records
    :param chunk_size: Read and return up to this size (in bytes) in the stream parser
    """

    def __init__(self, response, raise_on_empty, chunk_size=1024):
        self._raise_on_empty = raise_on_empty
        self._response = response
        self._chunk_size = chunk_size
        self._count = 0

    @property
    def count(self):
        return self._count

    @count.setter
    def count(self, count):
        if not isinstance(count, int) or isinstance(count, bool):
            raise TypeError("Count must be an integer")

        self._count = count

    def __repr__(self):
        return '<%s [%d - %s]>' % (self.__class__.__name__, self._response.status_code, self._response.request.method)

    def _parse_response(self):
        """Looks for `result.item` (array), `result` (object) and `error` (object) keys and parses
        the raw response content (stream of bytes)

        :raise:
            - ResponseError: If there's an error in the response
            - NoResults: If empty result set and raise_on_empty is set to True
            - MissingResult: If no result nor error was found
        """

        has_result_single = False
        has_result_many = False
        has_error = False

        for prefix, event, value in ijson.parse(self._response.raw, buf_size=self._chunk_size):
            if (prefix, event) == ('error', 'start_map'):
                # Matched ServiceNow `error` object at the root
                has_error = True
                builder = ObjectBuilder()
            elif prefix == 'result' and event in ['start_map', 'start_array']:
                # Matched ServiceNow `result`
                builder = ObjectBuilder()
                if event == 'start_map':  # Matched object
                    has_result_single = True
                elif event == 'start_array':  # Matched array
                    has_result_many = True

            if has_result_many:
                # Build the result
                if (prefix, event) == ('result.item', 'end_map'):
                    # Reached end of object. Set count and yield
                    builder.event(event, value)
                    self.count += 1
                    yield getattr(builder, 'value')
                elif prefix.startswith('result.item'):
                    # Build the result object
                    builder.event(event, value)
            elif has_result_single:
                if (prefix, event) == ('result', 'end_map'):
                    # Reached end of the result object. Set count and yield.
                    builder.event(event, value)
                    self.count += 1
                    yield getattr(builder, 'value')
                elif prefix.startswith('result'):
                    # Build the error object
                    builder.event(event, value)
            elif has_error:
                if (prefix, event) == ('error', 'end_map'):
                    # Reached end of the error object - raise ResponseError exception
                    raise ResponseError(getattr(builder, 'value'))
                elif prefix.startswith('error'):
                    # Build the error object
                    builder.event(event, value)

        if (has_result_single or has_result_many) and self.count == 0:  # Results empty
            if self._raise_on_empty is True:
                # Raise exception if it was requested
                raise NoResults('Query yielded no results')

            # Otherwise just yield empty dict
            yield {}

        if not (has_result_single or has_result_many or has_error):  # None of the expected keys were found
            raise MissingResult('The expected `result` key was missing in the response. Cannot continue')

    def _get_validated_response(self):
        """Validates response then calls :meth:`_parse_response` to yield content

        Immediately yields response content if request method is DELETE and code 204 (this response never
        contains a body).

        :raise:
            - NoResults: On status 404 if raise_on_empty is set to True
        """

        response = self._response

        if response.request.method == 'DELETE' and response.status_code == 204:
            yield [{'status': 'record deleted'}]
        else:
            try:
                # Raise an HTTPError if we hit a non-200 status code
                response.raise_for_status()
            except HTTPError as e:
                # Versions prior to Helsinki returns 404 on empty result sets
                if response.status_code == 404:
                    if self._raise_on_empty is True:
                        raise NoResults('Query yielded no results')
                    else:
                        yield [{}]
                else:
                    raise e

            # Parse byte stream
            yield self._parse_response()

    def all(self):
        """Returns a chained generator response containing all matching records

        :return: Iterable response
        """

        return chain.from_iterable(self._get_validated_response())

    def first(self):
        """Return the first record or raise an exception if the result doesn't contain any data

        :return: Dictionary containing the first item in the response content
        :raise:
            - NoResults: If no results were found
        """

        self._raise_on_empty = True

        return next(self.all())

    def first_or_none(self):
        """Return the first record or None

        :return: Dictionary containing the first item or None
        """

        try:
            return self.first()
        except NoResults:
            return None

    def one(self):
        """Return exactly one result or raise an exception.

        :return: Dictionary containing the only item in the response content
        :raise:
            - MultipleResults: If more than one records are present in the content
            - NoResults: If no records are present in the content
        """

        self._raise_on_empty = True

        r = self.all()
        result = next(r)

        try:
            next(r)
        except StopIteration:
            pass
        else:
            raise MultipleResults("Expected single-record result, got multiple")

        return result

    def one_or_none(self):
        """Return at most one result or raise an exception.

        :return: Dictionary containing the matching record or None
        :raise:
            - MultipleResults: If more than one records are present in the content
        """

        try:
            return self.one()
        except NoResults:
            return None
