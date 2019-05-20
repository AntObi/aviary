import torch
import torch.nn as nn
from torch_scatter import scatter_max, scatter_mean, \
                            scatter_add, scatter_mul
import copy

class MessageLayer(nn.Module):
    """
    Class defining the message passing operation on the composition graph
    """
    def __init__(self, atom_fea_len, nbr_fea_len, atom_gate):
        """
        Inputs
        ----------
        atom_fea_len: int
            Number of atom hidden features.
        nbr_fea_len: int
            Number of bond features.
        """
        super(MessageLayer, self).__init__()
        self.atom_fea_len = atom_fea_len
        self.nbr_fea_len = nbr_fea_len
        
        self.filter_msg = nn.Linear(2*self.atom_fea_len+self.nbr_fea_len, self.atom_fea_len)
        self.filter_bn = nn.BatchNorm1d(self.atom_fea_len)
        self.filter_act = nn.Sigmoid()

        self.core_msg = nn.Linear(2*self.atom_fea_len+self.nbr_fea_len, self.atom_fea_len)
        self.core_bn = nn.BatchNorm1d(self.atom_fea_len)
        self.core_act = nn.ELU()

        # self.pooling = WeightedMeanPooling()
        self.pooling = GlobalAttention(atom_gate)

        self.out_act = nn.ELU()



    def forward(self, atom_weights, atom_in_fea, bond_nbr_fea, 
                self_fea_idx, nbr_fea_idx):
        """
        Forward pass

        Parameters
        ----------
        N: Total number of atoms (nodes) in the batch
        M: Total number of bonds (edges) in the batch
        C: Total number of crystals (graphs) in the batch

        Inputs
        ----------
        atom_in_fea: Variable(torch.Tensor) shape (N, atom_fea_len)
            Atom hidden features before message passing
        bond_nbr_fea: Variable(torch.Tensor) shape (M, nbr_fea_len)
            Bond features of atom's neighbours
        self_fea_idx: torch.Tensor shape (M,)
            Indices of M neighbours of each atom
        nbr_fea_idx: torch.Tensor shape (M,)
            Indices of M neighbours of each atom
        atom_bond_idx: list of torch.Tensor of length N
            mapping from the atom idx to bond idx

        Returns
        -------
        atom_out_fea: nn.Variable shape (N, atom_fea_len)
            Atom hidden features after message passing
        """
        # construct the total features for passing
        atom_nbr_weights = atom_weights[nbr_fea_idx,:]
        atom_nbr_fea = atom_in_fea[nbr_fea_idx, :]
        atom_self_fea = atom_in_fea[self_fea_idx,:]

        total_fea = torch.cat([atom_self_fea, atom_nbr_fea, bond_nbr_fea], dim=1)

        filter_fea = self.filter_msg(total_fea)
        filter_fea = self.filter_bn(filter_fea)
        filter_fea = self.filter_act(filter_fea)

        core_fea = self.core_msg(total_fea)
        core_fea = self.core_bn(core_fea)
        core_fea = self.core_act(core_fea)

        # take the elementwise product of the filter and core
        nbr_message = filter_fea * core_fea
        # nbr_message = filter_fea 
        # nbr_message = core_fea

        # sum selectivity over the neighbours to get atoms
        out = self.pooling(nbr_message, self_fea_idx, atom_nbr_weights)

        # out = torch.cat([atom_in_fea, out], dim=1)
        
        return out

    def __repr__(self):
        return '{}'.format(self.__class__.__name__)

      
class CompositionNet(nn.Module):
    """
    Create a neural network for predicting total material properties.

    The CompositionNet model is comprised of a fully connected network
    and message passing graph layers.

    The message passing layers are used to determine a descriptor set
    for the fully connected network. Critically the graphs are used to 
    represent (crystalline) materials in a structure agnostic manner 
    but contain trainable parameters unlike other structure agnostic
    approaches.
    """
    def __init__(self, orig_atom_fea_len, nbr_fea_len,
                 atom_gate, crys_gate, 
                 atom_fea_len, n_graph, 
                 output_nn=None):
        """
        Initialize CompositionNet.

        Parameters
        ----------
        n_h: Number of hidden layers after pooling

        Inputs
        ----------
        orig_atom_fea_len: int
            Number of atom features in the input.
        nbr_fea_len: int
            Number of bond features.
        atom_fea_len: int
            Number of hidden atom features in the graph layers
        n_graph: int
            Number of graph layers
        """
        super(CompositionNet, self).__init__()

        # apply linear transform to the input features to get a trainable embedding
        self.embedding = nn.Linear(orig_atom_fea_len, atom_fea_len)

        # create a list of Message passing layers
        self.graphs = nn.ModuleList([MessageLayer(atom_fea_len=atom_fea_len,
                                                 nbr_fea_len=nbr_fea_len,
                                                 atom_gate=copy.deepcopy(atom_gate))
                                    for i in range(n_graph)])

        # self.pooling = WeightedMeanPooling()
        self.pooling = GlobalAttention(crys_gate)

        if output_nn:
            self.output_nn = output_nn

    def forward(self, atom_weights, orig_atom_fea, nbr_fea, self_fea_idx, 
                nbr_fea_idx, crystal_atom_idx):
        """
        Forward pass

        Parameters
        ----------
        N: Total number of atoms (nodes) in the batch
        M: Total number of bonds (edges) in the batch
        C: Total number of crystals (graphs) in the batch

        Inputs
        ----------
        orig_atom_fea: Variable(torch.Tensor) shape (N, orig_atom_fea_len)
            Atom features of each of the N atoms in the batch
        nbr_fea: Variable(torch.Tensor) shape (M, nbr_fea_len)
            Bond features of each M bonds in the batch
        self_fea_idx: torch.Tensor shape (M,)
            Indices of the atom each of the M bonds correspond to
        nbr_fea_idx: torch.Tensor shape (M,)
            Indices of of the neighbours of the M bonds connect to
        atom_bond_idx: list of torch.LongTensor of length C
            Mapping from the bond idx to atom idx
        crystal_atom_idx: list of torch.LongTensor of length C
            Mapping from the atom idx to crystal idx
        
        Returns
        -------
        out: nn.Variable shape (C,)
            Atom hidden features after message passing
        """

        # embed the original features into the graph layer description
        atom_fea = self.embedding(orig_atom_fea)

        # apply the graph message passing functions 
        for graph_func in self.graphs:
            atom_fea = graph_func(atom_weights, atom_fea, nbr_fea, self_fea_idx, nbr_fea_idx)

        # generate crystal features by pooling the atomic features
        crys_fea = self.pooling(atom_fea, crystal_atom_idx, atom_weights)

        if self.output_nn:
            crys_fea = self.output_nn(crys_fea)

        return crys_fea

    def __repr__(self):
        return '{}'.format(self.__class__.__name__)


class WeightedMeanPooling(nn.Module):
    """
    mean pooling
    """
    def __init__(self):
        super(WeightedMeanPooling, self).__init__()

    def forward(self, x, index, weights):
        weights = weights.unsqueeze(-1) if weights.dim() == 1 else weights
        x = weights * x 

        weighted_mean = scatter_mul(x, index, dim=0)/scatter_mul(weights, index, dim=0)

        return weighted_mean

    def __repr__(self):
        return '{}'.format(self.__class__.__name__)
        
class GlobalAttention(nn.Module):
    """  Weighted softmax attention layer  """
    def __init__(self, gate_nn):
        super(GlobalAttention, self).__init__()
        self.gate_nn = gate_nn

    def forward(self, x, index, weights):
        """ forward pass """
        x = x.unsqueeze(-1) if x.dim() == 1 else x

        gate = self.gate_nn(x).view(-1,1)
        assert gate.dim() == x.dim() and gate.size(0) == x.size(0)

        gate = gate - scatter_max(gate, index, dim=0)[0][index]
        gate = weights * gate.exp() 
        gate = gate / (scatter_add(gate, index, dim=0)[index] + 1e-13)

        out = scatter_add(gate * x, index, dim=0)

        return out

    def __repr__(self):
        return '{}(gate_nn={})'.format(self.__class__.__name__,
                                              self.gate_nn)
