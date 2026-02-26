"""
Helpers and mockups for ERE tests.
"""

import datetime
import hashlib
from logging import getLogger
from pathlib import Path
from typing import Dict, Generator, Iterable

from assertpy import assert_that
from rdflib import Graph

from ere.adapters import AbstractResolver
from ere.adapters.redis import AbstractClient
from erspec.models.ere import ( 
	ERERequest, EREResponse, 
	EntityMentionResolutionRequest, EntityMentionResolutionResponse,
	# FullRebuildRequest, FullRebuildResponse,  # TODO: uncomment when available
	EREErrorResponse,
)
from erspec.models.core import (
	ClusterReference,
	EntityMentionIdentifier
)

log = getLogger ( __name__ )

ERS_TEST_DATA_NS = "https://data.europa.eu/ers/resource/"
ERS_SCHEMA_NS = "https://data.europa.eu/ers/schema/"

EPD_NS = "http://data.europa.eu/a4g/resource/"
EPO_NS = "http://data.europa.eu/a4g/ontology#"
ORG_NS = "http://www.w3.org/ns/org#"


class MockEREClient ( AbstractClient ):
	"""
	A Mockup ERE client, based on an internal in-memory store loaded with test data.
	"""
	def __init__ ( self ):
		self._init_test_data ()
		self._response_queue = []
	
	def _init_test_data ( self ):
		self._resolver = MockResolver ()

	def push_request ( self, request: ERERequest ):
		result = self._resolver.process_request ( request )
		self._response_queue.append ( result )

	def subscribe_responses ( self ) -> Generator[EREResponse, None, None]:
		while self._response_queue:
			yield self._response_queue.pop ( 0 )


# TODO: will become an internal class for the implementation
class _ERECluster:
	def __init__ ( 
		self,
		uri: str, 
		members: Dict [str, float] = {}
	):
		self.uri = uri
		self.members = members



class MockResolver ( AbstractResolver ):
	"""
	A mockup in-memory resolver for entity resolution, based on test data.
	"""

	SUPPORTED_ENTITY_TYPES = { f"{ORG_NS}Organization", f"{EPO_NS}Procedure" }

	def __init__ ( self ):
		self._load_test_data ()
		self._extract_all_clusters ()
	
	def get_member_clusters ( self, member_uri: str ) -> list[tuple[str, float]]:
		"""
		Returns: a list of tuples of (cluster URI, confidence score) for the entity URI.
		"""
		clusters = self._member_index.get ( member_uri )
		if not clusters: return []
		result = [ (cluster.uri, cluster.members [ member_uri ]) for cluster in clusters ]

		return result
	
	def get_cluster_by_entity ( self, entity_uri: str ) -> _ERECluster:
		cluster = self._canonical_entity_index.get ( entity_uri )
		if cluster: return cluster
		return self._member_index.get ( entity_uri )
	
	def process_request ( self, request: ERERequest ) -> EREResponse:
		"""
		Dispatches a request to the appropriate handler.

		This is also responsible for wrapping any exception into an :class:`EREErrorResponse`.
		"""

		try:
			# TODO: this is an initial silly implementation, which violates the Open/Closed principle, move
			# it to an abstract method for a resolution service and have a default implementation 
			# based on a registry
			if isinstance ( request, EntityMentionResolutionRequest ):
				return self.resolve_entity ( request )
			# TODO: Uncomment when FullRebuildRequest is available in erspec
			# elif isinstance ( request, FullRebuildRequest ):
			# 	return self.process_full_rebuild_request ( request )
			else:
				raise ValueError ( f'Unsupported request type: { type ( request ) }' )
			
		except Exception as ex:
			log.error ( f"Error processing request { request.ere_request_id }: { ex }", exc_info = True )
			ex_type = type ( ex )
			ex_name = ex_type.__name__
			
			ex_fqn_name = ex_type.__module__
			if ex_fqn_name == 'builtins': ex_fqn_name = ''
			if ex_fqn_name: ex_fqn_name += "."
			ex_fqn_name += ex_name
			
			req_type = type ( request ).__name__

			error_response = EREErrorResponse (
				ere_request_id = request.ere_request_id,
				error_title = f"Request processing error: { str ( ex ) }",
				error_detail = f"{ex_name} Error while processing request of type { req_type }: { str ( ex ) }",
				error_type = ex_fqn_name
			)
			return error_response


	def resolve_entity ( self, request: EntityMentionResolutionRequest ) -> EntityMentionResolutionResponse:
		"""
		Mocks up an entity resolution, that is:

		TODO: rewrite this comment!
		"""

		entity_id = request.entityMention.identifier

		# It's not useful here, but we need to test error responses.
		entity_type = request.entityMention.identifier.entityType
		if entity_type not in self.SUPPORTED_ENTITY_TYPES:
			raise ValueError ( f"MockResolver, unsupported entity type: '{ entity_type }'" )

		entity_uri = entity_id_2_uri ( entity_id )

		candidate_clusters = self.get_member_clusters ( entity_uri )
		if not candidate_clusters:
			# OK, this goes into a new singleton cluster.
			new_cluster_uri = entity_id_2_cluster_uri ( entity_id )
			self._create_new_cluster ( new_cluster_uri, members = { entity_uri: 1.0 } )
			
			# I know it's already here, but let's ensure the creation works
			candidate_clusters = self.get_member_clusters ( entity_uri )
		
		# Sort them
		candidate_clusters.sort ( key = lambda x: x [ 1 ], reverse = True )

		# TODO: low-confidence filter

		if not candidate_clusters:
			raise RuntimeError ( f'Internal error during mock entity resolution for entity { entity_uri }: cluster not found or created' )

		# Transform them into model objects
		candidate_clusters = [
			ClusterReference ( clusterId = clusterId, confidenceScore = score ) for clusterId, score in candidate_clusters 
		]

		result = EntityMentionResolutionResponse (
			ere_request_id = request.ere_request_id,
			entityMentionId = entity_id,
			candidates = candidate_clusters,
			timestamp = create_timestamp ()
		)
		return result


	# TODO: Uncomment when FullRebuildRequest/Response are available in erspec
	# def process_full_rebuild_request ( self, request ) -> FullRebuildResponse:
	# 	"""
	# 	Mocks up the processing of a rebuild request by reloading the test data.
	# 	"""
	# 	# Reset to the initial test data, getting rid of new clusters created via requests after initialisation.
	# 	self.__init__ ()
	#
	# 	# And then we're done
	# 	response = FullRebuildResponse ( 
	# 		ere_request_id = request.ere_request_id,
	# 		timestamp = create_timestamp ()
	# 	)
	# 	return response


	def _load_test_data ( self ):
		"""
		Populates the internal RDF graph with data from test files.
		"""

		self.graph = Graph ()
		test_dir = Path ( __file__ ).parent.parent / 'resources'

		for ttl_file in test_dir.glob ( 'example*.ttl' ):
			# TODO: logging
			print ( f'Loading test data from { ttl_file }' )
			self.graph.parse ( str ( ttl_file ), format = 'turtle' )

	def _create_new_cluster (
		self, 
		cluster_uri: str = None,
		members: Dict [ str, float ] = {}
	) -> _ERECluster:
		"""
		Creates a new cluster for the given entity and updates the internal data with it.

		Returns: the created ERECluster instance, which can be used to add members.
		"""
		cluster = _ERECluster ( cluster_uri, members )
		# We also need an index from member URIs to clusters
		for member_uri in members.keys ():
			if member_uri not in self._member_index:
				self._member_index [ member_uri ] = []
			self._member_index [ member_uri ].append ( cluster )

		return cluster


	def _extract_all_clusters ( self ) -> Dict[str, _ERECluster]:
		"""
		Extracts cluster info from test data like:

		epd:id_2023-S-210-662860_ReviewerOrganisation_LLhJHMi9mby8ixbkfyGoWj_Cluster
			a ers:Cluster;
			ers:membership [
				ers:member epd:id_2023-S-210-661238_ReviewerOrganisation_LLhJHMi9mby8ixbkfyGoWj;
				ers:confidence 1.0 # Canonical entity
			],
			[...]
		.

		Returns: an index from member URIs to ERECluster instances.
		"""

		def extract_members ( cluster_uri: str ) -> Dict[str, float]:
			"""
			Extracts the members of a cluster from the RDF graph, given the cluster URI.
			
			Returns: a dict of member URI to confidence score.
			"""
			
			members = {}
			query = f"""
			PREFIX ers:		<{ERS_SCHEMA_NS}>
			SELECT ?member ?confidence WHERE {{
				<{ cluster_uri }> ers:membership ?membership .
				?membership ers:member ?member ;
										ers:confidence ?confidence .
			}}
			"""
			for row in self.graph.query ( query ):
				member_uri = str ( row['member'] )
				score = float ( row['confidence'] )
				members [ member_uri ] = score

			return members

		self._member_index: Dict[str, list[_ERECluster]] = {}

		query = f"""
		PREFIX ers:		<{ERS_SCHEMA_NS}>

		SELECT ?cluster WHERE {{
			?cluster a ers:Cluster .
		}}
		"""

		for row in self.graph.query ( query ):
			cluster_uri = str ( row [ 'cluster' ] )
			print ( f"Loading cluster { cluster_uri }" )
			members = extract_members ( cluster_uri )

			self._create_new_cluster ( cluster_uri, members	)

		if not self._member_index:
			raise ValueError ( 'No clusters found in the test data' )
		
	# /end: _extract_all_clusters ()
	

def hash_uri ( uri: str ) -> str:
	"""
	Generates a simple hash for URIs to be used for tasks like generating a cluster URI

	TODO: is it still needed?
	TODO: utils module
	"""
	
	return hashlib.md5 ( uri.encode ( 'utf-8' ) ).hexdigest ()


def extract_resource_rdf ( graph: Graph, resource_uri: str ) -> Graph:
	"""
	Fetches subject-centric triples from the test data, up to a couple of levels deep.

	TODO: do we still need it?
	"""
	
	sparql = """
	CONSTRUCT {
		?myent ?p ?o.
		?o ?p1 ?o1.
		?o1 ?p2 ?o2
	}
	WHERE {
		bind ( <%s> AS ?myent )
		?myent ?p ?o.

		OPTIONAL { 
			?o ?p1 ?o1. 
			OPTIONAL { ?o1 ?p2 ?o2. }
		}
	}
	"""
	sparql = sparql % resource_uri
	entity_graph = graph.query ( sparql ).graph
	if len ( entity_graph ) == 0:
		raise ValueError ( f'No RDF found for entity { resource_uri }' )
	return entity_graph
# /end: _extract_entity_rdf ()


def catch_response (
	ere_cli: AbstractClient, request_id: str, type_to_check: type[EREResponse] = None 
) -> EREResponse:
	"""
	Subscribes to to ERE responses and keeps getting responses until one with the given
	request ID is found.

	If the response flow stops (eg, channel closed, system went down), raises a :class:`RuntimeError`
	
	If type_to_check isn't None, asserts that the response is an instance of the given type.	
	"""

	for response in ere_cli.subscribe_responses ():
		if response.ere_request_id == request_id:
			if type_to_check:
				assert_that ( response, f"Response for request ID '{request_id}' is of the expected type" )\
					.is_instance_of ( type_to_check )			
			return response
	raise RuntimeError ( f"No response found for request ID '{request_id}'" )


def entity_id_2_uri ( entity_id: EntityMentionIdentifier ) -> str:
	"""
	Gets an entity URI from the entity mention ID. 

	This works under the mock-up data conventions, ie, the entity mention ID has the entity URI as its
	`requestId` field.

	Later, we will complement this with a real implementation.
	"""
	return entity_id.requestId

def entity_id_2_cluster_uri ( entity_id: EntityMentionIdentifier ) -> str:
	"""
	Gets a cluster URI from the entity mention ID. 

	This works under the mock-up data conventions, ie, when a new singleton cluster is created,
	its URI is :function:`entity_id_2_uri` plus a postfix, which means (by the same conventions), 
	it's the requested entity's URI plus a postfix.

	Later, we will complement this with a real implementation.
	"""
	entity_uri = entity_id_2_uri ( entity_id )
	return f'{entity_uri}_Cluster'


def create_timestamp () -> str:
	"""
	Factorises the timestamp generation for responses, yielding an ISO-formatted now.

	TODO: to be moved to a utils module.
	"""
	return datetime.datetime.now( datetime.UTC ).isoformat()


def prefix_common_namespaces ( rdf_or_sparql_body: str ) -> str:
	"""
	Simple helper to have your Turtle or SPARQL string prefixed with common namespace prefixes.

	TODO: do we still need it?
	"""
	return """
		PREFIX cccev: <http://data.europa.eu/m8g/>
		PREFIX dct:   <http://purl.org/dc/terms/>
		PREFIX ep:    <http://eprints.org/ontology/>
		PREFIX epd:   <http://data.europa.eu/a4g/resource/>
		PREFIX epo:   <http://data.europa.eu/a4g/ontology#>
		PREFIX locn:  <http://www.w3.org/ns/locn#>
		PREFIX org:   <http://www.w3.org/ns/org#>
		PREFIX owl:   <http://www.w3.org/2002/07/owl#>
		PREFIX ql:    <http://semweb.mmlab.be/ns/ql#>
		PREFIX rdf:   <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
		PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
		PREFIX rml:   <http://semweb.mmlab.be/ns/rml#>
		PREFIX rr:    <http://www.w3.org/ns/r2rml#>
		PREFIX skos:  <http://www.w3.org/2004/02/skos/core#>
		PREFIX tedm:  <http://data.europa.eu/a4g/mapping/sf-rml/>
		PREFIX time:  <http://www.w3.org/2006/time#>
		PREFIX xsd:   <http://www.w3.org/2001/XMLSchema#>

	""" + rdf_or_sparql_body



