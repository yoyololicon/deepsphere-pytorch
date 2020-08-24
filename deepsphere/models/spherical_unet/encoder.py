"""Encoder for Spherical UNet.
"""
# pylint: disable=W0221
from torch import nn
from deepsphere.models.spherical_unet.utils import SphericalChebBN, SphericalChebBNPool, SphericalChebConv


class SphericalChebBN2(nn.Module):
    """Building Block made of 2 Building Blocks (convolution, batchnorm, activation).
    """

    def __init__(self, in_channels, middle_channels, out_channels, kernel_size, **kwargs):
        """Initialization.

        Args:
            in_channels (int): initial number of channels.
            middle_channels (int): middle number of channels.
            out_channels (int): output number of channels.
            lap (:obj:`torch.sparse.FloatTensor`): laplacian.
            kernel_size (int, optional): polynomial degree.
        """

        super().__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.spherical_cheb_bn_1 = SphericalChebBN(in_channels, middle_channels, kernel_size, **kwargs)
        self.spherical_cheb_bn_2 = SphericalChebBN(middle_channels, out_channels, kernel_size, **kwargs)

    def forward(self, x):
        """Forward Pass.

        Args:
            x (:obj:`torch.Tensor`): input [batch x vertices x channels/features]

        Returns:
            :obj:`torch.Tensor`: output [batch x vertices x channels/features]
        """
        x = self.spherical_cheb_bn_1(x)
        x = self.spherical_cheb_bn_2(x)
        return x


class SphericalChebPool(nn.Module):
    """Building Block with a pooling/unpooling and a Chebyshev Convolution.
    """

    def __init__(self, in_channels, out_channels, pooling, kernel_size, **kwargs):
        """Initialization.

        Args:
            in_channels (int): initial number of channels.
            out_channels (int): output number of channels.
            lap (:obj:`torch.sparse.FloatTensor`): laplacian.
            pooling (:obj:`torch.nn.Module`): pooling/unpooling module.
            kernel_size (int, optional): polynomial degree.
        """
        super().__init__()
        self.pooling = pooling
        self.spherical_cheb = SphericalChebConv(in_channels, out_channels, kernel_size,
                                                **kwargs)

    def forward(self, x):
        """Forward Pass.

        Args:
            x (:obj:`torch.Tensor`): input [batch x vertices x channels/features]

        Returns:
            :obj:`torch.Tensor`: output [batch x vertices x channels/features]
        """
        x = self.pooling(x)
        x = self.spherical_cheb(x)
        return x


class Encoder(nn.Module):
    """Encoder for the Spherical UNet.
    """

    def __init__(self, pooling, kernel_size, edge_index_list: list, edge_weight_list: list, laplacian_type):
        """Initialization.

        Args:
            pooling (:obj:`torch.nn.Module`): pooling layer.
            kernel_size (int): polynomial degree.
        """
        super().__init__()
        assert len(edge_index_list) == len(edge_weight_list) == 6
        self.pooling = pooling
        self.kernel_size = kernel_size
        self.enc_l5 = SphericalChebBN2(16, 32, 64, self.kernel_size,
                                       edge_index=edge_index_list[5],
                                       edge_weight=edge_weight_list[5],
                                       laplacian_type=laplacian_type)
        self.enc_l4 = SphericalChebBNPool(64, 128, self.pooling, self.kernel_size,
                                          edge_index=edge_index_list[4],
                                          edge_weight=edge_weight_list[4],
                                          laplacian_type=laplacian_type)
        self.enc_l3 = SphericalChebBNPool(128, 256, self.pooling, self.kernel_size,
                                          edge_index=edge_index_list[3],
                                          edge_weight=edge_weight_list[3],
                                          laplacian_type=laplacian_type)
        self.enc_l2 = SphericalChebBNPool(256, 512, self.pooling, self.kernel_size,
                                          edge_index=edge_index_list[2],
                                          edge_weight=edge_weight_list[2],
                                          laplacian_type=laplacian_type)
        self.enc_l1 = SphericalChebBNPool(512, 512, self.pooling, self.kernel_size,
                                          edge_index=edge_index_list[1],
                                          edge_weight=edge_weight_list[1],
                                          laplacian_type=laplacian_type)
        self.enc_l0 = SphericalChebPool(512, 512, self.pooling, self.kernel_size,
                                        edge_index=edge_index_list[0],
                                        edge_weight=edge_weight_list[0],
                                        laplacian_type=laplacian_type)

    def forward(self, x):
        """Forward Pass.

        Args:
            x (:obj:`torch.Tensor`): input [batch x vertices x channels/features]

        Returns:
            x_enc* :obj: `torch.Tensor`: output [batch x vertices x channels/features]
        """
        x_enc5 = self.enc_l5(x)
        x_enc4 = self.enc_l4(x_enc5)
        x_enc3 = self.enc_l3(x_enc4)
        x_enc2 = self.enc_l2(x_enc3)
        x_enc1 = self.enc_l1(x_enc2)
        x_enc0 = self.enc_l0(x_enc1)

        return x_enc0, x_enc1, x_enc2, x_enc3, x_enc4


class EncoderTemporalConv(Encoder):
    """Encoder for the Spherical UNet temporality with convolution.
    """

    def __init__(self, pooling, sequence_length, kernel_size, **kwargs):
        """Initialization.

        Args:
            pooling (:obj:`torch.nn.Module`): pooling layer.
            laps (list): List of laplacians.
            sequence_length (int): The number of images used per sample.
            kernel_size (int): Polynomial degree.
        """
        super().__init__(pooling, kernel_size, **kwargs)
        self.sequence_length = sequence_length
        self.enc_l5 = SphericalChebBN2(
            self.enc_l5.in_channels * self.sequence_length,
            self.enc_l5.in_channels * self.sequence_length,
            self.enc_l5.out_channels,
            kernel_size,
            edge_index=kwargs['edge_index_list'][-1],
            edge_weight=kwargs['edge_weight_list'][-1],
            laplacian_type=kwargs['laplacian_type']
        )
