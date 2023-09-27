import jax
import jax.numpy as jnp
from jax.tree_util import Partial

from functools import cache, partial

from updec.config import DIM
from updec.utils import make_nodal_rbf, make_monomial, compute_nb_monomials, make_all_monomials
from updec.cloud import Cloud


def assemble_Phi(cloud:Cloud, rbf:callable=None):
    """ Assemble the matrix Phi (see equation 5) from Shahane """
    ## TODO: Make this matrix sparse. Only consider local supports
    ## rbf could be a string instead

    N, Ni, Nd = cloud.N, cloud.Ni, cloud.Nd
    Phi = jnp.zeros((N, N))
    # nodal_rbf = jax.jit(partial(make_nodal_rbf, rbf=rbf))         
    # nodal_rbf = Partial(make_nodal_rbf, rbf=rbf)                    ## TODO Use the prexisting nodal_rbf func
    rbf_vec = jax.vmap(rbf, in_axes=(None, 0), out_axes=0)
    grad_rbf = jax.grad(rbf)
    grad_rbf_vec = jax.vmap(grad_rbf, in_axes=(None, 0), out_axes=0)

    nodes = cloud.sorted_nodes

    # for i in range(N):      ## TODO only from 0 to Ni+Nd
    # # for i in range(Ni+Nd):
    #     # for j in cloud.local_supports[i]:
    #     #     Phi = Phi.at[i, j].set(nodal_rbf(cloud.nodes[i], cloud.nodes[j]))

    #     support_ids = jnp.array(cloud.local_supports[i])
    #     Phi = Phi.at[i, support_ids].set(rbf_vec(nodes[i], nodes[support_ids]))

    def rbf_body_func(i, Phi):
        support_ids = cloud.sorted_local_supports[i]
        return Phi.at[i, support_ids].set(rbf_vec(nodes[i], nodes[support_ids]))

    Phi = jax.lax.fori_loop(0, N, rbf_body_func, Phi)


    # for i in range(Ni+Nd, N):
    #     assert cloud.node_types[i] in ["n"], "not a neumann boundary node"    ## Internal nod

    #     support_n = jnp.array(cloud.local_supports[i])
    #     grads = jnp.nan_to_num(grad_rbf_vec(nodes[i], nodes[support_n]), neginf=0., posinf=0.)
    #     Phi = Phi.at[i, support_n].set(jnp.dot(grads, cloud.outward_normals[i]))

    # print("Finiteness Phi:", jnp.all(jnp.isfinite(Phi)))
    # print("Last column Phi all zero?", jnp.allclose(Phi[:,-1], 0.))
    # print("matrix Phi:\n", Phi)     ## Indicates singularity
    # print("Determinant of matrix Phi:", jnp.linalg.det(Phi))     ## Indicates singularity

    return Phi


def assemble_P(cloud:Cloud, nb_monomials:int):
    """ See (6) from Shanane """
    N = cloud.N
    M = nb_monomials
    P = jnp.zeros((N, M))
    nodes = cloud.sorted_nodes

    for j in range(M):
        # monomial = jax.jit(Partial(make_monomial, id=j))      ## IS
        monomial = Partial(make_monomial, id=j)
        monomial_vec = jax.vmap(monomial, in_axes=(0,), out_axes=0)
        P = P.at[:, j].set(monomial_vec(nodes))

        # for i in range(N):
        #     P = P.at[i, j].set(monomial(cloud.nodes[i]))
    ###  print("Finiteness P:", jnp.all(jnp.isfinite(P)))

    # def monomial_body_func(j, P):
    #     monomial = Partial(make_monomial, id=j)
    #     monomial_vec = jax.vmap(monomial, in_axes=(0,), out_axes=0)
    #     return P.at[:, j].set(monomial_vec(nodes))
    # P = jax.lax.fori_loop(0, M, monomial_body_func, P)

    return P


# @cache        ## TODO Make caching work with jax.jit ! Should be OK with jitting the whole PDE solver
def assemble_A(cloud, rbf, nb_monomials=2):
    """ Assemble matrix A, see (4) from Shanane """

    Phi = assemble_Phi(cloud, rbf)
    P = assemble_P(cloud, nb_monomials)

    N, M = Phi.shape[1], P.shape[1]

    A = jnp.zeros((N+M, N+M))
    A = A.at[:N, :N].set(Phi)
    A = A.at[:N, N:].set(P)
    A = A.at[N:, :N].set(P.T)

    return A

# @cache          ## Turn this into assemble and LU decompose
def assemble_invert_A(cloud, rbf, nb_monomials):
    A = assemble_A(cloud, rbf, nb_monomials)
    ### print("Determinant of matrix A:", jnp.linalg.det(A))     ## Indicates singularity
    ### print("Invert:", jnp.linalg.inv(A))
    return jnp.linalg.inv(A)


def assemble_op_Phi_P(operator:callable, cloud:Cloud, rbf:callable, nb_monomials:int, args:list):
    """ Assembles upper op(Phi): the collocation matrix to which a differential operator is applied """
    ## Only the internal nodes (M, N)

    # operator = jax.jit(operator, static_argnums=2)

    N = cloud.N
    Ni = cloud.Ni
    M = nb_monomials
    opPhi = jnp.zeros((Ni, N))
    opP = jnp.zeros((Ni, M))

    nodes = cloud.sorted_nodes
    # if len(args) > 0:
    #     # fields = jnp.stack(args, axis=-1)
    #     fields = jnp.stack(args, axis=-1)
    # else:
    #     fields = jnp.ones((N,1))     ## TODO Won't be used tho. FIx this !
    fields = jnp.stack(args, axis=-1) if args else jnp.ones((N,1))      ## TODO Find a better way. Will never be used

    # operator_rbf = partial(operator, monomial=None)
    # @jax.jit
    def operator_rbf(x, center=None, args=None):
        return operator(x, center, rbf, None, args)
    operator_rbf_vec = jax.jit(jax.vmap(operator_rbf, in_axes=(None, 0, None), out_axes=0))

    # operator_mon = Partial(operator, node=None)
    def operator_mon(x, args=None, monomial=None):
        return operator(x, None, rbf, monomial, args)
    monomials = make_all_monomials(M)

    # coords = cloud.sorted_nodes
    internal_ids = jnp.arange(Ni)

    # for i in range(Ni):
    #     assert cloud.node_types[i] == "i", "not an internal node"    ## Internal node

    #     support_ids = jnp.array(cloud.local_supports[i])
    #     opPhi = opPhi.at[i, support_ids].set(operator_rbf_vec(nodes[i], nodes[support_ids], fields[i]))

    def rbf_body_func(i, opPhi):
        # assert cloud.node_types[i] == "i", "not an internal node"    ## Internal node
        support_ids = cloud.sorted_local_supports[i]
        return opPhi.at[i, support_ids].set(operator_rbf_vec(nodes[i], nodes[support_ids], fields[i]))

    opPhi = jax.lax.fori_loop(0, Ni, rbf_body_func, opPhi)

    # opPhi = jax.lax.fori_loop(0, Ni, lambda i, Phi: rbf_body_func(i, opPhi, cloud, nodes, fields, operator_rbf_vec), opPhi)


    for j in range(M):
        operator_mon_func = Partial(operator_mon, monomial=monomials[j])
        operator_mon_vec = jax.vmap(operator_mon_func, in_axes=(0, 0), out_axes=0)
        opP = opP.at[internal_ids, j].set(operator_mon_vec(nodes[internal_ids], fields[internal_ids]))

    # def mon_body_func(j, opP):
    #     operator_mon_func = Partial(operator_mon, monomial=monomials[j])
    #     operator_mon_vec = jax.vmap(operator_mon_func, in_axes=(0, 0), out_axes=0)
    #     opP = opP.at[internal_ids, j].set(operator_mon_vec(nodes[internal_ids], fields[internal_ids]))

    # opP = jax.lax.fori_loop(0, M, mon_body_func, opP)

    ### print("Finiteness op Phi and P:", jnp.all(jnp.isfinite(opPhi)), jnp.all(jnp.isfinite(opP)))
    return opPhi, opP



def assemble_bd_Phi_P(cloud:Cloud, rbf:callable, nb_monomials:int, robin_coeffs:dict=None):

    """ Assembles upper op(Phi): the collocation matrix to which a differential operator is applied """
    ## Only the internal nodes (M, N)

    # operator = jax.jit(operator, static_argnums=2)

    N, Ni = cloud.N, cloud.Ni
    Nd, Nn, Nr = cloud.Nd, cloud.Nn, cloud.Nr
    M = nb_monomials
    bdPhi = jnp.zeros((Nd+Nn+Nr, N))
    bdP = jnp.zeros((Nd+Nn+Nr, M))

    # rbf = Partial(make_rbf, rbf=rbf)                    ## TODO JIT THIS, and Use the prexisting rbf func
    grad_rbf = jax.grad(rbf)
    # grad_rbf = jax.jit(jax.grad(nodal_rbf))

    rbf_vec = jax.vmap(rbf, in_axes=(None, 0), out_axes=0)
    grad_rbf_vec = jax.vmap(grad_rbf, in_axes=(None, 0), out_axes=0)

    nodes = cloud.sorted_nodes


    # ## Fill Matrix Phi with vectorisation from axis=1 ###

    # for i in range(Ni, N):
    #     ii = i-Ni        ## Actual index in the matrices
    #     assert cloud.node_types[i] in ["d", "n", "r"], "not a boundary node"    ## Internal node

    #     support = jnp.array(cloud.local_supports[i])
    #     vals = rbf_vec(nodes[i], nodes[support])
    #     grads = jnp.nan_to_num(grad_rbf_vec(nodes[i], nodes[support]), neginf=0., posinf=0.)

    #     if cloud.node_types[i] == "d":
    #         bdPhi = bdPhi.at[ii, support].set(vals)

    #     elif cloud.node_types[i] == "n":    ## Neumann node
    #         bdPhi = bdPhi.at[ii, support].set(jnp.dot(grads, cloud.outward_normals[i]))

    #     elif cloud.node_types[i] == "r":    ## Robin node
    #         betas_js = robin_coeffs[i]*jnp.ones(support.shape[0])
    #         bdPhi = bdPhi.at[ii, support].set(betas_js*vals + jnp.dot(grads, cloud.outward_normals[i]))



    def bdPhi_d_body_func(i, bdPhi):
        # assert cloud.node_types[i] in ["d", "n", "r"], "not a boundary node"    ## Internal node

        support = cloud.sorted_local_supports[i]
        vals = rbf_vec(nodes[i], nodes[support])

        return bdPhi.at[i-Ni, support].set(vals)

    bdPhi = jax.lax.fori_loop(Ni, Ni+Nd, bdPhi_d_body_func, bdPhi)

    def bdPhi_n_body_func(i, bdPhi):
        # assert cloud.node_types[i] in ["d", "n", "r"], "not a boundary node"    ## Internal node

        support = cloud.sorted_local_supports[i]
        grads = jnp.nan_to_num(grad_rbf_vec(nodes[i], nodes[support]), neginf=0., posinf=0.)

        if hasattr(cloud, "sorted_outward_normals"):
            normals = cloud.sorted_outward_normals[i-Ni-Nd]
        else:
            normals = jnp.zeros((DIM,))

        ## PROBLEM: index i must be shifted when outward normals becomes an array. Also, what if no outward normal are defined?
        return bdPhi.at[i-Ni, support].set(jnp.dot(grads, normals))

    bdPhi = jax.lax.fori_loop(Ni+Nd, Ni+Nd+Nn, bdPhi_n_body_func, bdPhi)

    ## TODO: SOrt thisfirst
    sorted_robin_coeffs = cloud.sort_dict_by_keys(robin_coeffs) if len(robin_coeffs) > 0 else None   ## For JIT. Since their ids are contiguous by construction. TODO write a full test for this

    def bdPhi_r_body_func(i, bdPhi):
        # assert cloud.node_types[i] in ["d", "n", "r"], "not a boundary node"    ## Internal node

        support = cloud.sorted_local_supports[i]
        vals = rbf_vec(nodes[i], nodes[support])
        grads = jnp.nan_to_num(grad_rbf_vec(nodes[i], nodes[support]), neginf=0., posinf=0.)

        if sorted_robin_coeffs is not None:       ## Compile time const
            betas_js = sorted_robin_coeffs[i-Ni-Nd-Nn]*jnp.ones(support.shape[0])
        else:
            betas_js = jnp.zeros(support.shape[0])

        if hasattr(cloud, "sorted_outward_normals"):
            normals = cloud.sorted_outward_normals[i-Ni-Nd-Nn]
        else:
            normals = jnp.zeros((DIM,)) ## This brach shoudl never be taken TODO

        return bdPhi.at[i-Ni, support].set(betas_js*vals + jnp.dot(grads, normals))

    bdPhi = jax.lax.fori_loop(Ni+Nd+Nn, N, bdPhi_r_body_func, bdPhi)





    ### Fill Matrix P with vectorisation from axis=0 ###
    node_ids_d = [k for k,v in cloud.node_types.items() if v == "d"]
    node_ids_n = [k for k,v in cloud.node_types.items() if v == "n"]
    node_ids_r = [k for k,v in cloud.node_types.items() if v == "r"]
    betas_is = [robin_coeffs[k] for k,v in cloud.node_types.items() if v == "r"]

    monomials = make_all_monomials(M)
    if len(node_ids_d) > 0:
        node_ids_d = jnp.array(node_ids_d)
        for j in range(M):
            monomial_vec = jax.vmap(monomials[j], in_axes=(0,), out_axes=0)
            bdP = bdP.at[node_ids_d-Ni, j].set(monomial_vec(nodes[node_ids_d]))

        # def dirichlet_body_func(j, bdP):
        #     monomial_vec = jax.vmap(monomials[j], in_axes=(0,), out_axes=0)
        #     return bdP.at[node_ids_d-Ni, j].set(monomial_vec(nodes[node_ids_d]))
        # bdP = jax.lax.fori_loop(0, M, dirichlet_body_func, bdP)

    if len(node_ids_n) > 0:
        normals_n = jnp.stack([cloud.outward_normals[i] for i in node_ids_n], axis=0)
        node_ids_n = jnp.array(node_ids_n)

        dot_vec = jax.vmap(jnp.dot, in_axes=(0,0), out_axes=0)
        for j in range(M):
            grad_monomial = jax.grad(monomials[j])
            grad_monomial_vec = jax.vmap(grad_monomial, in_axes=(0,), out_axes=0)
            grads = grad_monomial_vec(nodes[node_ids_n])
            bdP = bdP.at[node_ids_n-Ni, j].set(dot_vec(grads, normals_n))

        # def neumann_body_func(j, bdP):
        #     grad_monomial = jax.grad(monomials[j])
        #     grad_monomial_vec = jax.vmap(grad_monomial, in_axes=(0,), out_axes=0)
        #     grads = grad_monomial_vec(nodes[node_ids_n])
        #     return bdP.at[node_ids_n-Ni, j].set(dot_vec(grads, normals_n))
        # bdP = jax.lax.fori_loop(0, M, neumann_body_func, bdP)

    if len(node_ids_r) > 0:
        normals_r = jnp.stack([cloud.outward_normals[i] for i in node_ids_r], axis=0)
        node_ids_r = jnp.array(node_ids_r)

        dot_vec = jax.vmap(jnp.dot, in_axes=(0,0), out_axes=0)
        for j in range(M):
            monomial_vec = jax.vmap(monomials[j], in_axes=(0,), out_axes=0)
            vals = monomial_vec(nodes[node_ids_r])

            grad_monomial = jax.grad(monomials[j])
            grad_monomial_vec = jax.vmap(grad_monomial, in_axes=(0,), out_axes=0)
            grads = grad_monomial_vec(nodes[node_ids_r])

            bdP = bdP.at[node_ids_r-Ni, j].set(jnp.array(betas_is)*vals + dot_vec(grads, normals_r))

        # def robin_body_func(j, bdP):
        #     monomial_vec = jax.vmap(monomials[j], in_axes=(0,), out_axes=0)
        #     vals = monomial_vec(nodes[node_ids_r])

        #     grad_monomial = jax.grad(monomials[j])
        #     grad_monomial_vec = jax.vmap(grad_monomial, in_axes=(0,), out_axes=0)
        #     grads = grad_monomial_vec(nodes[node_ids_r])

        #     return bdP.at[node_ids_r-Ni, j].set(jnp.array(betas_is)*vals + dot_vec(grads, normals_r))
        # bdP = jax.lax.fori_loop(0, M, robin_body_func, bdP)

    ### print("Finiteness bd Phi and P:", jnp.all(jnp.isfinite(bdPhi)), jnp.all(jnp.isfinite(bdP)))

    return bdPhi, bdP



def assemble_B(operator:callable, cloud:Cloud, rbf:callable, nb_monomials:int, diff_args:list, robin_coeffs:dict):
    """ Assemble B using opPhi, P, and A """

    N, Ni = cloud.N, cloud.Ni
    # M = compute_nb_monomials(max_degree, 2)
    M = nb_monomials

    # Phi, P = assemble_Phi(cloud, rbf), assemble_P(cloud, M)
    # rbf = Partial(make_rbf, rbf=rbf)

    ## Compute coefficients here

    opPhi, opP = assemble_op_Phi_P(operator, cloud, rbf, M, diff_args)
    bdPhi, bdP = assemble_bd_Phi_P(cloud, rbf, M, robin_coeffs)

    full_opPhi = jnp.zeros((N, N))
    full_opP = jnp.zeros((N, M))

    full_opPhi = full_opPhi.at[:Ni, :].set(opPhi[:, :])
    full_opP = full_opP.at[:Ni, :].set(opP[:, :])

    full_opPhi = full_opPhi.at[Ni:, :].set(bdPhi[:, :])
    full_opP = full_opP.at[Ni:, :].set(bdP[:, :])

    diffMat = jnp.concatenate((full_opPhi, full_opP), axis=-1)

    # A = assemble_A(cloud, nodal_rbf, M)       ## TODO make this work for nodal_rbf
    # A = assemble_A(cloud, rbf, M)

    inv_A = assemble_invert_A(cloud, rbf, M)
    B = diffMat @ inv_A

    return B[:, :N]


def new_compute_coefficients(field:jnp.ndarray, cloud:Cloud, rbf:callable, nb_monomials:int):
    """ Find nodal and polynomial coefficients for scalar field """ 

    rhs = jnp.concatenate((field, jnp.zeros((nb_monomials))))
    inv_A = assemble_invert_A(cloud, rbf, nb_monomials)

    return inv_A@rhs



def assemble_q(operator:callable, boundary_conditions:dict, cloud:Cloud, rbf:callable, nb_monomials:int, rhs_args:list):
    """ Assemble the right hand side q using the operator """
    ### Boundary conditions should match all the types of boundaries

    N = cloud.N
    Ni = cloud.Ni
    M = nb_monomials
    q = jnp.zeros((N,))

    ## Compute coefficients here
    if rhs_args != None:
        fields_coeffs = [new_compute_coefficients(field, cloud, rbf, M) for field in rhs_args]
        fields_coeffs = jnp.stack(fields_coeffs, axis=-1)
    else:
        fields_coeffs = None

    ## Internal node
    operator_vec = jax.vmap(operator, in_axes=(0, None, None, None), out_axes=(0))
    nodes = cloud.sorted_nodes
    internal_ids = jnp.arange(Ni)
    q = q.at[internal_ids].set(operator_vec(nodes[internal_ids], nodes, rbf, fields_coeffs))


    ## Facet nodes
    for f_id in cloud.facet_types.keys():
        assert f_id in boundary_conditions.keys(), "facets and boundary functions don't match ids"

        bd_op = boundary_conditions[f_id]
        bd_node_ids = jnp.array(cloud.facet_nodes[f_id])

        if callable(bd_op):      ## Is a (jitted) function 
            bd_op_vec = jax.vmap(bd_op, in_axes=(0,), out_axes=0)
            q = q.at[bd_node_ids].set(bd_op_vec(nodes[bd_node_ids]))
        else:                   ## Must be a jax array then
            q = q.at[bd_node_ids].set(bd_op)

    # keys = list(cloud.facet_types.keys())
    # def rhs_body_func(i, q):
    #     f_id = keys[i]
    #     assert f_id in boundary_conditions.keys(), "facets and boundary functions don't match ids"

    #     bd_op = boundary_conditions[f_id]
    #     bd_node_ids = jnp.array(cloud.facet_nodes[f_id])

    #     if callable(bd_op):      ## Is a (jitted) function 
    #         bd_op_vec = jax.vmap(bd_op, in_axes=(0,), out_axes=0)
    #         q = q.at[bd_node_ids].set(bd_op_vec(nodes[bd_node_ids]))
    #     else:                   ## Must be a jax array then
    #         q = q.at[bd_node_ids].set(bd_op)

    #     return q
    # q = jax.lax.fori_loop(0, len(keys), rhs_body_func, q)

    return q
