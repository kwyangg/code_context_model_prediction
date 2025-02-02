import copy
import random

import dgl
import math
import numpy as np
import torch
from dgl import DGLGraph
from torch.nn.parameter import Parameter
from torch.nn.modules.module import Module
from scipy.spatial.distance import pdist, squareform
import torch.nn.functional as F


class Decoder(Module):
    """
    根据特征生成邻接矩阵，对应论文中使用权重点积来生成边信息
    adopt a vanilla design, weighted inner production
    """

    def __init__(self, nembed, dropout=0.1):
        super(Decoder, self).__init__()
        self.dropout = dropout
        self.de_weight = Parameter(torch.FloatTensor(nembed, nembed))  # 权重矩阵
        # self.de_weight1 = Parameter(torch.FloatTensor(nembed, nembed))  # 权重矩阵
        self.reset_parameters()

    def reset_parameters(self):
        stdv = 1. / math.sqrt(self.de_weight.size(1))
        self.de_weight.data.uniform_(-stdv, stdv)
        # self.de_weight1.data.uniform_(-stdv, stdv)

    def forward(self, node_embed):
        combine = F.linear(node_embed, self.de_weight)
        # combine = F.relu(combine)
        # combine = F.linear(combine, self.de_weight1)
        adj_out = torch.sigmoid(torch.mm(combine, combine.transpose(-1, -2)))
        return adj_out


def smote_sample(embed: torch.Tensor, labels: torch.Tensor, idx_train: torch.Tensor, adj: torch.Tensor,
                 portion=1.0) -> tuple[
    torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
    """
    使用 smote 进行节点插值生成节点

    :param embed：原始节点的嵌入矩阵。
    :param labels：节点的标签。
    :param idx_train：训练集的节点索引。
    :param adj：邻接矩阵（可选）。
    :param portion：从每个类别选择的节点的比例, default 1

    # 示例数据
    embed = torch.randn(10, 5)  # 10个节点，每个节点有5维嵌入
    labels = torch.tensor([0, 0, 1, 1, 2, 2, 2, 3, 3, 3])
    adj = torch.randint(0, 2, (10, 10)).float()
    形状为 (10, 10) 的张量，张量中的元素是从 0 到 1（包括 0 和 1）的随机整数，并将这些整数转换为浮点数
    """
    # 通过将训练集样本总数除以类别总数，可以得到每个类别平均分配的样本数量
    pos_num = (labels == 1).sum().item()
    neg_num = (labels == 0).sum().item()
    adj_new = None

    # 针对需要进行采样的类别分别进行采样，我们这里设置为 1 即可
    chosen = idx_train[(labels == 1)[idx_train]]
    # 计算需要采样的个数和轮数 将正样本翻倍，但是不要超过负样本
    num = min(int(chosen.shape[0] * portion), neg_num - pos_num)
    c_portion = 1

    if num <= 0:
        return embed, labels, idx_train, adj

    for j in range(c_portion):
        chosen = chosen[:num]
        chosen_embed = embed[chosen, :]
        # 寻找最近邻插值
        # pdist 计算每对节点之间的成对距离，返回一个一维距离数组。
        # squareform 将这个一维距离数组转换为二维距离矩阵，使得 distance[i, j] 表示第 i 个节点和第 j 个节点之间的距离。
        distance = squareform(pdist(chosen_embed.cpu().detach()))
        # 用一个很大的数填充距离矩阵的对角线，防止在寻找最近邻时把自己作为最近邻。
        np.fill_diagonal(distance, distance.max() + 100)
        # 返回每个节点的最近邻的索引，即在距离矩阵中每行最小值的位置索引。
        idx_neighbor = distance.argmin(axis=-1)
        # with the nearest neighbor, generate synthetic nodes
        interp_place = random.random()
        new_embed = embed[chosen, :] + (chosen_embed[idx_neighbor, :] - embed[chosen, :]) * interp_place
        new_labels = labels.new(torch.Size((chosen.shape[0], 1))).reshape(-1).fill_(1)
        idx_new = np.arange(embed.shape[0], embed.shape[0] + chosen.shape[0])
        idx_train_append = idx_train.new(idx_new)
        # 更新数据
        embed = torch.cat((embed, new_embed), 0)
        labels = torch.cat((labels, new_labels), 0)
        idx_train = torch.cat((idx_train, idx_train_append), 0)
        # 更新邻接矩阵
        if adj_new is None:
            adj_new = adj.new(torch.clamp_(adj[chosen, :] + adj[idx_neighbor, :], min=0.0, max=1.0))
        else:
            temp = adj.new(torch.clamp_(adj[chosen, :] + adj[idx_neighbor, :], min=0.0, max=1.0))
            adj_new = torch.cat((adj_new, temp), 0)
    add_num = adj_new.shape[0]
    new_adj = adj.new(torch.Size((adj.shape[0] + add_num, adj.shape[0] + add_num))).fill_(0.0)
    new_adj[:adj.shape[0], :adj.shape[0]] = adj[:, :]
    new_adj[adj.shape[0]:, :adj.shape[0]] = adj_new[:, :]
    new_adj[:adj.shape[0], adj.shape[0]:] = torch.transpose(adj_new, 0, 1)[:, :]
    return embed, labels, idx_train, new_adj.detach()


def adj_mse_loss(adj_rec, adj_tgt):
    """
    计算所有非合成节点的边预测损失，均方误差（MSE）损失

    :param adj_rec: 使用 edge generator 预测之后的邻接矩阵
    :param adj_tgt: 原始的邻接矩阵
    """
    edge_num = adj_tgt.nonzero().shape[0]
    total_num = adj_tgt.shape[0] ** 2
    # 负权重用于惩罚未观察到的边。边的数量除以不存在的边的数量。
    if total_num == edge_num:
        neg_weight = 1
    else:
        neg_weight = edge_num / (total_num - edge_num)
    weight_matrix = adj_rec.new(adj_tgt.shape).fill_(1.0)
    weight_matrix[adj_tgt == 0] = neg_weight
    loss = torch.sum(weight_matrix * (adj_rec - adj_tgt) ** 2)
    return loss


def graph_smote_sampling(graph: DGLGraph, features, decoder, device):
    # 使用上下文管理器设置设备
    with torch.cuda.device(device):
        labels = graph.ndata['label']  # 节点标签
        idx_train = torch.arange(graph.number_of_nodes()).cuda()
        # 提取边信息
        src, dst = graph.edges()
        num_nodes = graph.number_of_nodes()
        # 手动构建邻接矩阵
        adj = torch.zeros((num_nodes, num_nodes), dtype=torch.float32).cuda()
        adj[src, dst] = 1
        ori_num = labels.shape[0]

        embed, labels_new, idx_train_new, adj_up = smote_sample(features, labels, idx_train, adj.detach())
        # 使用带权重的点积
        generated_G = decoder(embed)
        # 不使用带权重的点积
        # generated_G = torch.sigmoid(torch.mm(embed, embed.transpose(-1, -2)))

        loss_smote = adj_mse_loss(generated_G[:ori_num, :][:, :ori_num], adj.detach())

        adj_new = copy.deepcopy(generated_G.detach())
        threshold = 0.5
        adj_new[adj_new < threshold] = 0.0
        adj_new[adj_new >= threshold] = 1.0
        adj_new = torch.mul(adj_up, adj_new)
        adj_new[:ori_num, :][:, :ori_num] = adj.detach()
        # 得到了新的信息，现在需要变回图去，新加入的边暂时都用 declares，label 赋值为 1
        # 存储值为1的元素的行和列索引
        rows = []
        cols = []
        # 遍历矩阵并排除左上角的m*m子矩阵
        for i in range(adj_new.size(0)):
            for j in range(adj_new.size(1)):
                if not (i < ori_num and j < ori_num):
                    if adj_new[i, j] == 1 or i == j:
                        rows.append(i)
                        cols.append(j)
        # 转换为tensor
        rows_tensor = torch.tensor(rows).cuda()
        cols_tensor = torch.tensor(cols).cuda()
        new_src = torch.cat((src, rows_tensor)).tolist()
        new_dst = torch.cat((dst, cols_tensor)).tolist()
        new_graph = dgl.graph(data=(new_src, new_dst), num_nodes=labels_new.shape[0]).to(device)
        new_graph.ndata['embedding'] = embed
        new_graph.ndata['label'] = labels_new
        new_num = labels_new.shape[0] - ori_num
        new_graph.ndata['kind'] = torch.cat(
            (graph.ndata['kind'], torch.full((new_num,), 4, dtype=graph.ndata['kind'].dtype).cuda()))
        new_edge = len(new_src) - src.shape[0]
        new_graph.edata['relation'] = torch.cat(
            (graph.edata['relation'], torch.full((new_edge,), 4, dtype=graph.edata['relation'].dtype).cuda()))
        return new_graph, loss_smote

# # 边的起点和终点列表
# src = [0, 0, 1, 2, 3, 4]
# dst = [1, 2, 3, 3, 4, 5]
#
# # 边的类型列表（0-3）
# edge_types = [0, 1, 2, 3, 0, 1]
#
# # 创建一个DGL图
# g = dgl.graph((src, dst))
#
# # 添加节点特征，假设每个节点有3维特征
# num_nodes = g.num_nodes()
# node_features = torch.randn(num_nodes, 3)
# g.ndata['embedding'] = node_features
#
# # 添加节点标签，假设标签为0或1
# node_labels = torch.randint(0, 2, (num_nodes,))
# g.ndata['label'] = node_labels
# node_labels = torch.randint(0, 4, (num_nodes,))
# g.ndata['kind'] = node_labels
# node_labels = torch.randint(0, 2, (num_nodes,))
# g.ndata['seed'] = node_labels
#
# # 添加边类型
# edge_types_tensor = torch.tensor(edge_types)
# g.edata['relation'] = edge_types_tensor
#
# # 打印图的信息
# print(g)
# print("节点特征:\n", g.ndata['embedding'])
# print("节点label:\n", g.ndata['label'])
# print("节点kind:\n", g.ndata['kind'])
# print("节点seed:\n", g.ndata['seed'])
# print("边类型:\n", g.edata['relation'])
# sample_graph = graph_smote_sampling(g)
# print(sample_graph)
# print("节点特征:\n", sample_graph.ndata['embedding'])
# print("节点label:\n", sample_graph.ndata['label'])
# print("节点kind:\n", sample_graph.ndata['kind'])
# print("节点seed:\n", sample_graph.ndata['seed'])
# print("边类型:\n", sample_graph.edata['relation'])
