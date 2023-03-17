##################################
# Author: Magnus Kvåle Helliesen #
##################################

import os
import numpy as np
import networkx as nx
from pyvis.network import Network
import pandas as pd
from symengine import var, Matrix, Lambdify
import matplotlib.pyplot as plt
from collections import Counter
from functools import cache


class ModelSolver:
    """
    EXAMPLE OF USE USE:

    Let "eqns" and "endo_vars" be lists with equations and endogenous variables, respectively, stored as strings. E.g.
        eqns = ['x+y=A', 'x-y=B']
        endo_vars = ['x', 'y']

    A class instance called "model" is initialized by

        model = MNAModel(eqns, endo_vars)
    This reads in the equations and endogenous variables and perform block analysis and ordering and generates simulation code.
    The model is then ready to be solved subject to data (exogenous and initial values of endogenous variables) in a Pandas dataframe.

    Let "data" be a dataframe containing data on A and B and initial values for x and y. Then the model can be solved by

        solution = model.solve_model(data)

    Now "solution" is a Pandas dataframe with exactly the same dimensions as "data", but where the endogenous variables are replaced by the solutions to the model.
    The last solution is also stored in "model.last_solution".

    Somethin about dependecy graphs...
    """

    def __init__(self, eqns: list, endo_vars: list):
        """
        Reads in equations and endogenous variables and does a number of operations, e.g. analyzing block structure using graph theory.
        Stores a number of results in instance variables.

        Args:
            eqns_list (list): List of equations equations as strings
            endog_vars_list (list): List of endogenous variables as strings

        Returns:
            None.
        """

        self._some_error = False
        self._lag_notation = '___LAG'
        self._max_lag = 0
        self._root_tolerance = 1e-7

        print('Initializing model...')

        # Model equations and endogenous variables are checked and stored as immutable tuples (as opposed to mutable lists)
        self._eqns, self._endo_vars = self._initialize_model(eqns, endo_vars)

        print('* Analyzing model...')

        # Analyzing equation strings to determine variables, lags and coefficients
        self._eqns_analyzed, self._var_mapping, self._lag_mapping = self._analyze_eqns()

        # Using graph theory to analyze equations using existing algorithms to establish minimum simultaneous blocks
        self._eqns_endo_vars_bigraph = self._gen_eqns_endo_vars_bigraph()
        self._eqns_endo_vars_match = self._find_max_bipartite_match()
        self._model_digraph = self._gen_model_digraph()
        self._condenced_model_digraph, self.condenced_model_node_varlist_mapping = self._gen_condenced_model_digraph()
        self._augmented_condenced_model_digraph = self._gen_augmented_condenced_model_digraph()

        # Generating everything needed to simulate model
        self._simulation_code, self._blocks = self._gen_simulation_code()

        print('Finished')


    @property
    def eqns(self):
        return self._eqns

    @property
    def endo_vars(self):
        return self._endo_vars

    @property
    def max_lag(self):
        return self._max_lag

    @property
    def blocks(self):
        return tuple(tuple([endo_vars, tuple(self._var_mapping.get(x)[2] for x in exog_vars), eqns]) for endo_vars, exog_vars, eqns in self._blocks)

    @property
    def root_tolerance(self):
        return self._root_tolerance

    @property
    def last_solution(self):
        try:
            return self._last_solution
        except AttributeError:
            print('ERROR: No solution exists')


    @root_tolerance.setter
    def root_tolerance(self, value):
        if type(value) != float:
            raise ValueError('ERROR: tolerance for termination must be of type float')
        if value <= 0:
            raise ValueError('ERROR: tolerance for termination must be positive')
        self._root_tolerance = value


    def _initialize_model(self, eqns: list, endo_vars: list):
        """
        Imports lists containing equations and endogenous variables stored as strings.
        Checks that there are no blank lines, sets everything to lowercase and returns as tuples.

        Args:
            eqns (list): List of equations
            endo_vars (list): List of endogenous variables
        Returns:
            Tuples containing equations and endogenous variables as strings.
        """

        print('* Importing equations')
        for i, eqn in enumerate(eqns):
            if eqn.strip() == '':
                self._some_error = True
                raise ValueError('ERROR: There are blank lines in equation list')
            eqns[i] = eqns[i].lower()

        print('* Importing endogenous variables')
        for endo_var in endo_vars:
            if endo_var.strip() == '':
                self._some_error = True
                raise ValueError('ERROR: There are blank lines in endogenous variable list')
            endo_vars[i] = endo_vars[i].lower()

        return tuple(eqns), tuple(endo_vars)


    def _analyze_eqns(self):
        """
        Returns equations and list of variables with and without lag notation.
        (-)-syntax is replaced with ___LAG_NOTATION.

        Returns:
            1) A list of equations and variables in equations with and without lag-notation.
            2) A mapping linking variables with (-)-notation to variable names and lags.
        """

        if self._some_error:
            return None, None

        print('\t* Analyzing equation strings')

        eqns_analyzed = []

        var_mapping, lag_mapping = {}, {}
        for eqn in self._eqns:
            eqn_analyzed = [eqn, *self._analyze_eqn(eqn)]
            eqns_analyzed += eqn_analyzed,
            var_mapping = {**var_mapping, **eqn_analyzed[2]}
            lag_mapping = {**lag_mapping, **eqn_analyzed[3]}

        return tuple(eqns_analyzed), var_mapping, lag_mapping


    def _analyze_eqn(self, eqn: str):
        """
        Takes an equation string and parses it into numerics (special care is taken to deal with scientific notation), variables, lags and operators/brackets.
        I've written my own parser in stead of using some existing because it needs to take care of then (-)-notation for lags.

        Args:
            equation (str): String containing equation.
        Returns:
            1) An equation string with (-)-syntax replaced by LAG_NOTATION-syntax for lagged variables (e.g. 'x(-1)' --> 'xLAG_NOTATION1').
            2) A list of lists containing pairs of variables in the equation and variables in the equation with (-)-syntax replaced by LAG_NOTATION-syntax for lagged variables.
            3) A mapping linking variables with (-)-notation to variable names and lags.
        """

        if self._some_error:
            return

        parsed_eqn_with_lag_notation, var_mapping, lag_mapping = [], {}, {}
        num, var, lag = '', '', ''
        is_num, is_var, is_lag, is_sci = False, False, False, False

        for chr in ''.join([eqn, ' ']):
            is_num = (chr.isnumeric() and not is_var) or is_num
            is_var = (chr.isalpha()  and not is_num) or is_var
            is_lag = (is_var and chr == '(') or is_lag
            is_sci = (is_num and chr == 'e') or is_sci

            # Check if character is something other than a numeric, variable or lag and write numeric or variable to parsed equation
            if chr in ['=','+','-','*','/','(',')',' '] and not (is_lag or is_sci):
                if is_num:
                    parsed_eqn_with_lag_notation += str(num),
                if is_var:
                    # Replace (-)-notation by LAG_NOTATION for lags and appends _ to the end to mark the end
                    pfx = '' if lag == '' else ''.join([self._lag_notation, str(-int(lag[1:-1])), '_'])
                    parsed_eqn_with_lag_notation += ''.join([var, pfx]),
                    var_mapping[''.join([var, lag])] = ''.join([var, pfx])
                    var_mapping[''.join([var, pfx])] = ''.join([var, lag])
                    lag_mapping[''.join([var, pfx])] = (var, 0 if lag == '' else -int(lag[1:-1]))
                    if lag != '':
                        self._max_lag = max(self._max_lag, -int(lag.replace('(', '').replace(')', '')))
                if chr != ' ':
                    parsed_eqn_with_lag_notation += chr,
                num, var, lag = '', '', ''
                is_num, is_var, is_lag = False, False, False
                continue

            if is_sci and chr.isnumeric():
                is_sci = False

            if is_num:
                num = ''.join([num, chr])
                continue

            if is_var and not is_lag:
                var = ''.join([var, chr])
                continue

            if is_var and is_lag:
                lag = ''.join([lag, chr])
                if chr == ')':
                    is_lag = False

        eqn_with_lag_notation=''.join(parsed_eqn_with_lag_notation)

        return eqn_with_lag_notation, var_mapping, lag_mapping


    def _gen_eqns_endo_vars_bigraph(self):
        """
        Generates bipartite graph connetcting equations (U) with endogenous variables (V).
        See https://en.wikipedia.org/wiki/Bipartite_graph for an explanation of what a bipartite graph is.

        Returns:
            Bipartite graph.
        """

        if self._some_error:
            return

        print('\t* Generating bipartite graph connecting equations and endogenous variables')

        # Make nodes in bipartite graph with equations U (0) and endogenous variables in V (1)
        eqns_endo_vars_bigraph = nx.Graph()
        eqns_endo_vars_bigraph.add_nodes_from([i for i, _ in enumerate(self._eqns)], bipartite=0)
        eqns_endo_vars_bigraph.add_nodes_from(self._endo_vars, bipartite=1)

        # Make edges between equations and endogenous variables
        for i, eqns in enumerate(self._eqns_analyzed):
            for endo_var in [x for x in eqns[2].keys() if x in self._endo_vars]:
                eqns_endo_vars_bigraph.add_edge(i, endo_var)

        return eqns_endo_vars_bigraph


    def _find_max_bipartite_match(self):
        """
        Finds a maximum bipartite match (MBM) of bipartite graph connetcting equations (U) with endogenous variables (V).
        See https://www.geeksforgeeks.org/maximum-bipartite-matching/ for more on MBM.

        Returns:
            Dictionary with matches (both ways, i.e. U-->V and U-->U).
        """

        if self._some_error:
            return

        print('\t* Finding maximum bipartite match (MBM) (i.e. associating every equation with exactly one endogenus variable)')

        # Use maximum bipartite matching to make a one to one mapping between equations and endogenous variables
        try:
            maximum_bipartite_match = nx.bipartite.maximum_matching(self._eqns_endo_vars_bigraph, [i for i, _ in enumerate(self._eqns)])
            if len(maximum_bipartite_match)/2 < len(self._eqns):
                self._some_error = True
                print('ERROR: Model is over or under spesified')
                return
        except nx.AmbiguousSolution:
            self._some_error = True
            print('ERROR: Unable to analyze model')
            return

        return maximum_bipartite_match


    def _gen_model_digraph(self):
        """
        Makes a directed graph showing how endogenous variables affect every other endogenous variable.
        See https://en.wikipedia.org/wiki/Directed_graph for more about directed graphs.

        Returns:
            Directed graph showing endogenous variables network.
        """

        if self._some_error:
            return

        print('\t* Generating directed graph (DiGraph) connecting endogenous variables using bipartite graph and MBM')

        # Make nodes in directed graph of endogenous variables
        model_digraph = nx.DiGraph()
        model_digraph.add_nodes_from(self._endo_vars)

        # Make directed edges showing how endogenous variables affect every other endogenous variables using bipartite graph and MBM
        for edge in self._eqns_endo_vars_bigraph.edges():
            if edge[0] != self._eqns_endo_vars_match[edge[1]]:
                model_digraph.add_edge(edge[1], self._eqns_endo_vars_match[edge[0]])

        return model_digraph


    def _gen_condenced_model_digraph(self):
        """
        Makes a condencation of directed graph of endogenous variables. Each node of condencation contains strongly connected components; this corresponds to the simulataneous model blocks.
        See https://en.wikipedia.org/wiki/Strongly_connected_component for more about strongly connected components.

        Returns:
            1) Condencation of directed graph of endogenous variables
            2) Mapping from condencation graph node --> variable list
        """

        if self._some_error:
            return

        print('\t* Finding condensation of DiGraph (i.e. finding minimum simulataneous equation blocks)')

        # Generate condensation graph of equation graph such that every node is a strong component of the equation graph
        condenced_model_digraph = nx.condensation(self._model_digraph)

        # Make a dictionary that associate every node of condensation with a list of variables
        node_vars_mapping = {}
        for node in tuple(condenced_model_digraph.nodes()):
            node_vars_mapping[node] = tuple(condenced_model_digraph.nodes[node]['members'])

        return condenced_model_digraph, node_vars_mapping


    def _gen_augmented_condenced_model_digraph(self):
        """
        Augments condencation graph with nodes and edges for exogenous variables in order to show what exogenous variables affect what strong components.

        Returns:
            Augmented condencation of directed graph of endogenous variables.
        """

        if self._some_error:
            return

        augmented_condenced_model_digraph = self._condenced_model_digraph.copy()

        # Make edges between exogenous variables and strong components it is a part of
        for node in self._condenced_model_digraph.nodes():
            for member in self._condenced_model_digraph.nodes[node]['members']:
                for exog_var_adjacent_to_node in [val for key, val in self._eqns_analyzed[self._eqns_endo_vars_match[member]][2].items()
                                                  if self._lag_notation not in val and key not in self._endo_vars]:
                    augmented_condenced_model_digraph.add_edge(exog_var_adjacent_to_node, node)

        return augmented_condenced_model_digraph


    def _gen_simulation_code(self):
        """
        TBA
        """

        if self._some_error:
            return

        print('\t* Generating simulation code (i.e. block-wise symbolic objective function, symbolic Jacobian matrix and lists of endogenous and exogenous variables)')

        simulation_code, blocks = [], []
        for node in reversed(tuple(self._condenced_model_digraph.nodes())):
            block_endo_vars, block_eqns_orig, block_eqns_lags, block_exog_vars = [], [], [], set()
            for member in self._condenced_model_digraph.nodes[node]['members']:
                i = self._eqns_endo_vars_match[member]
                eqns_analyzed = self._eqns_analyzed[i]
                block_endo_vars += member,
                block_eqns_orig += eqns_analyzed[0],
                block_eqns_lags += eqns_analyzed[1],
                block_exog_vars.update([val for key, val in eqns_analyzed[2].items() if self._lag_notation not in key])
            block_exog_vars.difference_update(set(block_endo_vars))

            blocks += tuple([tuple(block_endo_vars), tuple(block_exog_vars), tuple(block_eqns_orig)]),
            simulation_code += tuple([*self._gen_obj_fun_and_jac(tuple(block_eqns_lags), tuple(block_endo_vars), tuple(block_exog_vars)),
                tuple(block_endo_vars), tuple(block_exog_vars), tuple(block_eqns_lags)]),

        return tuple(simulation_code), tuple(blocks)


    @staticmethod
    def _gen_obj_fun_and_jac(eqns: tuple, endo_vars: tuple, exog_vars: tuple):
        """
        TBA
        """

        endo_symb, exog_symb, obj_fun = [], [], []
        for endo_var in endo_vars:
            var(endo_var)
            endo_symb += eval(endo_var),
        for exog_var in exog_vars:
            var(exog_var)
            exog_symb += eval(exog_var),
        for eqn in eqns:
            lhs, rhs = eqn.split('=')
            obj_fun_row = eval('-'.join([''.join(['(', lhs.strip().strip('+'), ')']), ''.join(['(', rhs.strip().strip('+'), ')'])]))
            obj_fun += obj_fun_row,

        jac = Matrix(obj_fun).jacobian(Matrix(endo_symb)).tolist()

        obj_fun_lambdify = Lambdify([*endo_symb, *exog_symb], obj_fun, cse=True)
        jac_lambdify = Lambdify([*endo_symb, *exog_symb], jac, cse=True)

        output_obj_fun = lambda val_list, *args: obj_fun_lambdify(*val_list, *args)
        output_jac = lambda val_list, *args: jac_lambdify(*val_list, *args)

        return output_obj_fun, output_jac


    def switch_endo_var(self, old_endo, new_endo):
        """
        TBA
        """

        pass


    def find_endo_var(self, endo_var):
        """
        TBA
        """

        try:
            return [endo_var in x[0] for x in self._blocks].index(True)
        except ValueError:
            return


    def show_model_info(self):
        """
        TBA
        """
        print('*'*100)
        print('Model consists of {} equations in {} blocks\n'.format(len(self._eqns), len(self._blocks)))
        for key, val in Counter(sorted([len(x[2]) for x in self._blocks])).items():
            print('{} blocks have {} equations'.format(val, key))
        print('*'*100)


    def show_blocks(self):
        """
        TBA
        """

        for i, _ in enumerate(self._blocks):
            print(' '.join(['*'*50, 'Block', str(i), '*'*50, '\n']))
            self.show_block(i)


    def show_block(self, i):
        """
        TBA
        """

        block = self._blocks[i]
        print('Endogenous ({} variables):'.format(len(block[0])))
        print('\n'.join([' '.join(x) for x in list(self._chunks(block[0], 25))]))
        print('\nExogenous ({} variables):'.format(len(block[1])))
        print('\n'.join([' '.join(x) for x in list(self._chunks([self._var_mapping.get(x) for x in block[1]], 25))]))
        print('\nEquations ({} equations):'.format(len(block[2])))
        print('\n'.join(block[2]))


    def solve_model(self, input_data: pd.DataFrame):
        """
        TBA
        """

        if self._some_error:
            return

        print('Solving model...')

        output_data_array = input_data.to_numpy(dtype=np.float64, copy=True)
        var_col_index = {var: i for i, var in enumerate(input_data.columns.str.lower().to_list())}

        print('\tFirst period: {}, last period: {}'.format(input_data.index[self._max_lag], input_data.index[output_data_array.shape[0]-1]))
        print('\tSolving', end=' ')

        for period in list(range(self._max_lag, output_data_array.shape[0])):
            print(input_data.index[period], end=' ')
            for i, simulation_code in enumerate(self._simulation_code):
                [obj_fun, jac, endo_vars, exog_vars, _] =  simulation_code
                solution = self._solve_block(
                    obj_fun,
                    jac,
                    endo_vars,
                    exog_vars,
                    tuple([output_data_array, var_col_index]),
                    period
                    )

                # If solution fails then print details about block and return
                if solution['status'] != 0:
                    print('\nERROR: Failed to solve block {}:'.format(i))
                    print(''.join(['Endogenous variables: ', ','.join(endo_vars)]))
                    print(','.join([str(x) for x in self._gen_endo_vals(endo_vars, output_data_array, var_col_index, period)]))
                    print(''.join(['Exogenous variables: ', ','.join(exog_vars)]))
                    print(','.join([str(x) for x in self._gen_exog_vals(exog_vars, output_data_array, var_col_index, period)]))
                if solution['status'] == 2:
                    return
                
                output_data_array[period, [var_col_index.get(x) for x in endo_vars]] = solution['x']

        print('\nFinished')

        self._last_solution = pd.DataFrame(output_data_array, columns=input_data.columns, index=input_data.index)

        return self._last_solution


    def _solve_block(self, obj_fun, jac, endo_vars: tuple, exog_vars: tuple, output_data_names: tuple, period: int):
        """
        TBA
        """

        output_data, var_col_index = output_data_names
        solution = self._newton_raphson(
            obj_fun,
            self._gen_endo_vals(endo_vars, output_data, var_col_index, period),
            args = self._gen_exog_vals(exog_vars, output_data, var_col_index, period),
            tol = self._root_tolerance,
            jac = jac
            )

        return solution


    def _gen_exog_vals(self, exog_vars: list, data: np.array, var_col_index: dict, period: int):
        """
        TBA
        """

        exog_var_vals = []
        for exog_var in exog_vars:
            exog_var_name, lag = self._lag_mapping.get(exog_var)
            exog_var_vals += self._fetch_cell(data, var_col_index.get(exog_var_name), period-lag),

        return tuple(exog_var_vals)


    def _gen_endo_vals(self, endo_vars: list, data: np.array, var_col_index: dict, period: int):
        """
        TBA
        """

        endo_var_vals = []
        for endo_var in endo_vars:
            endo_var_vals += self._fetch_cell(data, var_col_index.get(endo_var), period),

        return np.array(endo_var_vals, dtype=np.float64)


    @staticmethod
    def _fetch_cell(array, col, row):
        return array[row, col]


    @staticmethod
    def _newton_raphson(f, init, **kwargs):
        """
        TBA
        """

        if 'args' in kwargs:
            args = kwargs['args']
        else:
            args = ()
        if 'jac' in kwargs:
            jac = kwargs['jac']
        else:
            print('ERROR: Newton-Raphson requires symbolic Jacobian matrix')
            return {'x': np.array(init), 'fun': np.array(f(init, *args)), 'success': False}
        if 'tol' in kwargs:
            tol = kwargs['tol']
        else:
            tol = 1e-10
        if 'maxiter' in kwargs:
            maxiter = kwargs['maxiter']
        else:
            maxiter = 10

        success = True
        status = 0
        x_i = init
        f_i = np.array(f(init.tolist(), *args))
        i = 0
        while np.max(np.abs(f_i)) > 0:
            if i == maxiter:
                success = False
                status = 1
                break
            try:
                x_i_new = x_i-np.matmul(np.linalg.inv(np.array(jac(x_i.tolist(), *args))), f_i)
                if np.max(np.abs(x_i_new-x_i)) <= tol:
                    break
                x_i = x_i_new
            except np.linalg.LinAlgError:
                success = False
                status = 2
                break
            f_i = np.array(f(x_i, *args))
            i += 1

        return {'x': x_i, 'fun': f_i, 'success': success, 'status': status}


    def draw_blockwise_graph(self, variable: str, max_ancestor_generations: int, max_descentant_generations: int, max_nodes= int, in_notebook=False, html=False):
        """
        Draws a directed graph of block in which variable is along with max number of ancestors and descendants.
        Opens graph in browser.

        Args:
            variable (str): Variable who's block should be drawn
            max_ancestor_generations (int): Maximum number of anscestor blocks
            max_descentant_generations (int): Maximum number of descendant blocks

        Returns:
            None
        """

        if self._some_error:
            return

        if any([variable in self._condenced_model_digraph.nodes[x]['members'] for x in self._condenced_model_digraph.nodes()]):
            variable_node = [variable in self._condenced_model_digraph.nodes[x]['members'] for x in self._condenced_model_digraph.nodes()].index(True)
        elif variable in self._augmented_condenced_model_digraph.nodes(): 
            variable_node = variable
        else:
            raise NameError('Variable is not in model')

        ancr_nodes = nx.ancestors(self._augmented_condenced_model_digraph, variable_node)
        desc_nodes = nx.descendants(self._augmented_condenced_model_digraph, variable_node)

        max_ancr_nodes = {x for x in ancr_nodes if\
            nx.shortest_path_length(self._augmented_condenced_model_digraph, x, variable_node) <= max_ancestor_generations}
        max_desc_nodes = {x for x in desc_nodes if\
            nx.shortest_path_length(self._augmented_condenced_model_digraph, variable_node, x) <= max_descentant_generations}

        subgraph = self._augmented_condenced_model_digraph.subgraph({variable_node}.union(max_ancr_nodes).union(max_desc_nodes))
        graph_to_plot = nx.DiGraph()

        print(' '.join(['Graph of block containing {} with <={} generations of ancestors and <={} generations of decendants:'
                       .format(variable, max_ancestor_generations, max_descentant_generations), str(subgraph)]))

        # Loop over all nodes in subgraph (chosen variable, it's ancestors and decendants) and make nodes and edges in pyvis subgraph
        mapping = {}
        for node in subgraph.nodes():
            if node in self._condenced_model_digraph:
                node_label = '\n'.join(self.condenced_model_node_varlist_mapping[node])\
                    if len(self.condenced_model_node_varlist_mapping[node]) < 10 else '***\nHUGE BLOCK\n***'
                node_title = '<br>'.join(self.condenced_model_node_varlist_mapping[node])
                if node == variable_node:
                    node_size = 200
                    node_color = 'red'
                if node in max_ancr_nodes:
                    node_size = 100
                    node_color = 'green'
                if node in max_desc_nodes:
                    node_size = 100
                    node_color = 'blue'
            else:
                node_label = None
                node_title = None
                node_size = 100
                node_color = 'silver'
            graph_to_plot.add_node(node, label=node_label, title=node_title, shape='circle', size=node_size, color=node_color)
            if node in self.condenced_model_node_varlist_mapping:
                mapping[node] =  ':\n'.join([' '.join(['Block', str(len(self._blocks)-node-1)]),
                                             '\n'.join(self.condenced_model_node_varlist_mapping[node]) if len(self.condenced_model_node_varlist_mapping[node]) < 5 else '...'])
            else:
                mapping[node] = str(node)

        if graph_to_plot.number_of_nodes() > max_nodes:
            print('Graph is too big to plot')
            return

        graph_to_plot.add_edges_from(subgraph.edges())
        if html:
            net = Network('2000px', '2000px', directed=True, notebook=in_notebook)
            net.from_nx(graph_to_plot)
            net.repulsion(node_distance=50, central_gravity=0.01, spring_length=100, spring_strength=0.02, damping=0.5)
            net.show('graph.html')
        else:
            plt.figure(figsize=(5, 5))
            colors = [node[1]['color'] for node in graph_to_plot.nodes(data=True)]
            #layout = nx.planar_layout(subgraph_to_pyvis), node_size=2500, node_color=colors)
            #layout = nx.spring_layout(subgraph_to_pyvis, k=5/subgraph_to_pyvis.order()**0.5), node_size=2500, node_color=colors, font_size=9)
            #layout = nx.planar_layout(subgraph_to_pyvis), node_size=2500, node_color=colors, font_size=9)
            #layout = nx.spectral_layout(subgraph_to_pyvis)
            layout = nx.shell_layout(graph_to_plot)
            nx.draw(graph_to_plot, with_labels=True, labels=mapping, pos=layout, node_size=3000, node_color=colors, font_size=7, font_color='white')
            plt.plot()


    "https://stackoverflow.com/questions/312443/how-do-i-split-a-list-into-equally-sized-chunks"
    @staticmethod
    def _chunks(xs, n):
        n = max(1, n)
        return (xs[i:i+n] for i in range(0, len(xs), n))