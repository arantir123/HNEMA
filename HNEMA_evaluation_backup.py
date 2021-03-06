import time
import argparse
import torch
import torch.nn.functional as F
from torch.optim import lr_scheduler
from torch.nn.utils import clip_grad_norm_
import numpy as np
from sklearn.metrics import roc_auc_score, average_precision_score, precision_recall_curve, auc
from model.Auxiliary_networks import GIN4drug_struc, side_effect_predictor, therapeutic_effect_DNN_predictor
from model.HNEMA_link_prediction import HNEMA_link_prediction
from utils.pytorchtools import EarlyStopping
from utils.data import DrugStrucDataset, load_HNEMA_DDI_data_te
from utils.tools import index_generator, parse_minibatch
from sklearn.preprocessing import MinMaxScaler
from sklearn.metrics import mean_squared_error, mean_absolute_error
import random
import itertools
from dgl.dataloading import GraphDataLoader
from model.Auxiliary_networks import AutomaticWeightedLoss
import pandas as pd
import scipy.stats
import copy

# The HNE-GIN variant based on HNEMA

random_seed = 1024
random.seed(random_seed)
np.random.seed(random_seed)
torch.manual_seed(random_seed)
torch.cuda.manual_seed(random_seed)
torch.cuda.manual_seed_all(random_seed)
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = False
torch.backends.cudnn.enabled = False

# some overall fixed parameters
# drug/target/cell line
num_ntype = 3
dropout_rate = 0.5
lr = 0.005
weight_decay = 0.001

use_masks = [[False, False, False, True],
             [False, False, False, True]]

no_masks = [[False] * 4, [False] * 4]

num_drug = 159
num_target = 12575
# num_cellline = 20

involved_metapaths = [
    [(0, 1, 0), (0, 1, 1, 0), (0, 1, 1, 1, 0), (0, 'te', 0)]]

only_test=False

# the type of synergy score to be predicted
# S_mean, synergy_zip, synergy_loewe, synergy_hsa, synergy_bliss (corresponding to 0,1,2,3,4, respectively)
predicted_te_type = 2

def run_model_HNEMA_DDI(root_prefix, hidden_dim_main, num_heads_main, attnvec_dim_main, rnn_type_main,
                        num_epochs, patience, batch_size, neighbor_samples, repeat, attn_switch_main, rnn_concat_main,
                        hidden_dim_aux, loss_ratio_te, loss_ratio_se, layer_list, pred_in_dropout, pred_out_dropout, output_concat, args):

    print('current paramters:',loss_ratio_te, loss_ratio_se, output_concat, hidden_dim_aux, rnn_type_main)
    adjlists_ua, edge_metapath_indices_list_ua, adjM, type_mask, name2id_dict, train_val_test_drug_drug_samples, train_val_test_drug_drug_labels, all_drug_morgan = load_HNEMA_DDI_data_te(root_prefix)

    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
    # device = torch.device('cpu')

    features_list = []
    in_dims = []

    for i in range(num_ntype):
        dim = (type_mask == i).sum()
        in_dims.append(dim)
        indices = np.vstack((np.arange(dim), np.arange(dim)))
        indices = torch.LongTensor(indices)
        values = torch.FloatTensor(np.ones(dim))
        features_list.append(torch.sparse.FloatTensor(indices, values, torch.Size([dim, dim])).to(device))

    loss_ratio_te = torch.tensor(loss_ratio_te, dtype=torch.float32).to(device)
    loss_ratio_se = torch.tensor(loss_ratio_se, dtype=torch.float32).to(device)

    train_drug_drug_samples = train_val_test_drug_drug_samples['train_drug_drug_samples']

    # scaler = MinMaxScaler()
    train_te_temp_labels = train_val_test_drug_drug_labels['train_te_labels'][:, predicted_te_type].reshape(-1,1)
    # scaler.fit(train_te_temp_labels)
    # train_te_temp_labels = scaler.transform(train_te_temp_labels)
    train_te_labels = torch.tensor(train_te_temp_labels,dtype=torch.float32).to(device)
    train_se_labels = torch.tensor(train_val_test_drug_drug_labels['train_se_labels'],dtype=torch.float32).to(device)

    # an extra test about exchanging the val and test set
    val_drug_drug_samples = train_val_test_drug_drug_samples['val_drug_drug_samples']
    test_drug_drug_samples = train_val_test_drug_drug_samples['test_drug_drug_samples']

    val_te_temp_labels = train_val_test_drug_drug_labels['val_te_labels'][:, predicted_te_type].reshape(-1, 1)
    # test_te_temp_labels = scaler.transform(test_te_temp_labels)
    val_te_labels = torch.tensor(val_te_temp_labels,dtype=torch.float32).to(device)
    val_se_labels = torch.tensor(train_val_test_drug_drug_labels['val_se_labels'],dtype=torch.float32).to(device)

    test_te_temp_labels = train_val_test_drug_drug_labels['test_te_labels'][:, predicted_te_type].reshape(-1, 1)
    # val_te_temp_labels = scaler.transform(val_te_temp_labels)
    test_te_labels = torch.tensor(test_te_temp_labels,dtype=torch.float32).to(device)
    test_se_labels = torch.tensor(train_val_test_drug_drug_labels['test_se_labels'],dtype=torch.float32).to(device)

    # ?????????????????????????????????DGLdataloader,??????????????????Graphdataloader
    drug_info_set = DrugStrucDataset()
    # ?????????????????????????????????
    drug_struc_loader = GraphDataLoader(drug_info_set, batch_size=num_drug, drop_last=False)

    atomnum2id_dict = name2id_dict[-1]
    se_symbol2id_dict = name2id_dict[-2]
    cellline2id_dict = name2id_dict[-3]

    mse_list = []
    rmse_list = []
    mae_list = []
    pearson_list = []
    VAL_L0SS=[]
    for _ in range(repeat):
        main_net = HNEMA_link_prediction(
            [4], in_dims[:-1], hidden_dim_main, hidden_dim_main, num_heads_main, attnvec_dim_main, rnn_type_main,
            dropout_rate, attn_switch_main, rnn_concat_main, args)
        main_net.to(device)

        drug_net = GIN4drug_struc(len(atomnum2id_dict), hidden_dim_aux)
        drug_net.to(device)

        te_layer_list = copy.deepcopy(layer_list)
        te_layer_list.append(1)
        print('TE_layer_list:', te_layer_list)
        se_net = side_effect_predictor(hidden_dim_main + hidden_dim_aux, len(se_symbol2id_dict))
        se_net.to(device)
        # ?????????cell line/tissue type????????????????????????pair embedding????????????????????????cell line/tissue type embedding??????(?????????GIN?????????embedding??????)???????????????, ??????????????????se output, se output???????????????
        te_net = therapeutic_effect_DNN_predictor(len(cellline2id_dict), hidden_dim_main + hidden_dim_aux, hidden_dim_aux, te_layer_list, output_concat, len(se_symbol2id_dict), pred_out_dropout, pred_in_dropout)
        te_net.to(device)
        print('te_net:', te_net)
        sigmoid = torch.nn.Sigmoid()

        # optimizer = torch.optim.SGD(
        optimizer = torch.optim.Adam(
            itertools.chain(main_net.parameters(), drug_net.parameters(), te_net.parameters(), se_net.parameters()),
            lr=lr, weight_decay=weight_decay)

        scheduler = lr_scheduler.StepLR(optimizer, step_size=5, gamma=0.5)

        main_net.train()
        drug_net.train()
        se_net.train()
        te_net.train()

        if only_test == True:
            temp_prefix = './data/data4training_model/checkpoint/'
            model_save_path = temp_prefix + 'checkpoint.pt'
        else:
            model_save_path = root_prefix + 'checkpoint/checkpoint_{}.pt'.format(time.strftime("%Y_%m_%d_%H_%M_%S", time.localtime()))

        early_stopping = EarlyStopping(patience=patience, verbose=True,
                                       save_path=model_save_path)
        # three lists keeping the time of different training phases
        dur1 = []  # data processing before feeding data in an iteration
        dur2 = []  # the training time for an iteration
        dur3 = []  # the time to use grad to update parameters of the model

        train_sample_idx_generator = index_generator(batch_size=batch_size, num_data=len(train_drug_drug_samples))
        val_sample_idx_generator = index_generator(batch_size=batch_size//2, num_data=len(val_drug_drug_samples), shuffle=False)
        test_sample_idx_generator = index_generator(batch_size=batch_size//2, num_data=len(test_drug_drug_samples), shuffle=False)

        te_criterion = torch.nn.MSELoss(reduction='mean')
        se_criterion = torch.nn.BCELoss(reduction='mean')

        print('total epoch number is:',num_epochs)
        print('current loss_ratio_te and loss_ratio_se are:', loss_ratio_te, loss_ratio_se)
        if only_test==False:
            for epoch in range(num_epochs):
                t_start = time.time()
                main_net.train()
                drug_net.train()
                se_net.train()
                te_net.train()

                for iteration in range(train_sample_idx_generator.num_iterations()):
                    t0 = time.time()

                    train_sample_idx_batch = train_sample_idx_generator.next()
                    train_sample_idx_batch.sort()

                    train_drug_drug_batch = train_drug_drug_samples[train_sample_idx_batch].tolist()
                    train_te_labels_batch = train_te_labels[train_sample_idx_batch]
                    train_se_labels_batch = train_se_labels[train_sample_idx_batch]

                    train_drug_drug_idx = (np.array(train_drug_drug_batch)[:, :-1].astype(int)).tolist()
                    train_cellline_symbol = (np.array(train_drug_drug_batch)[:, -1]).tolist()
                    train_cellline_idx = [cellline2id_dict[i] for i in train_cellline_symbol]

                    train_g_lists, train_indices_lists, train_idx_batch_mapped_lists = parse_minibatch(
                        adjlists_ua, edge_metapath_indices_list_ua, train_drug_drug_idx, device, neighbor_samples,
                        use_masks, num_drug)

                    t1 = time.time()
                    dur1.append(t1 - t0)

                    # if the generated input data leads to many empty metapath-based subgraphs (in some batches), which will cause the error from CUDA+DGL
                    # please use the snippet below to skip these batches
                    # for mode in train_g_lists:
                    #     for metapath in mode:
                    #         if metapath.size()[0] == 0:
                    #             break_flag = True
                    #             break
                    #     if break_flag == True:
                    #         break
                    # if break_flag == True:
                    #     break_flag = False
                    #     continue

                    [row_drug_embedding, col_drug_embedding], _, [row_drug_atten, col_drug_atten] = main_net(
                        (train_g_lists, features_list, type_mask[:num_drug + num_target],
                         train_indices_lists, train_idx_batch_mapped_lists))

                    row_drug_batch, col_drug_batch = np.array(train_drug_drug_idx)[:, 0], np.array(train_drug_drug_idx)[:, 1]
                    drug_struc_input, drug_idx = iter(drug_struc_loader).next()
                    drug_atom_num = drug_struc_input.ndata['atom_num'].clone().detach().numpy()
                    drug_atom_num = torch.LongTensor([atomnum2id_dict[i] for i in drug_atom_num]).to(device)
                    drug_struc_input = drug_struc_input.to(device)
                    drug_struc_embedding = drug_net(drug_struc_input, drug_atom_num)
                    row_drug_struc_embedding, col_drug_struc_embedding = drug_struc_embedding[row_drug_batch], drug_struc_embedding[col_drug_batch]
                    row_drug_composite_embedding = torch.cat((row_drug_embedding, row_drug_struc_embedding), axis=1)
                    col_drug_composite_embedding = torch.cat((col_drug_embedding, col_drug_struc_embedding), axis=1)

                    se_output = sigmoid(se_net(row_drug_composite_embedding, col_drug_composite_embedding))
                    train_cellline_idx = torch.LongTensor(train_cellline_idx).to(device)
                    if output_concat==True:
                        se_output_ = se_output.clone().detach()
                        te_output = te_net(row_drug_composite_embedding, col_drug_composite_embedding, train_cellline_idx, se_output_)
                    else:
                        te_output = te_net(row_drug_composite_embedding, col_drug_composite_embedding, train_cellline_idx)

                    te_loss = te_criterion(te_output, train_te_labels_batch)
                    se_loss = se_criterion(se_output, train_se_labels_batch)
                    train_total_loss_batch = loss_ratio_te * te_loss + loss_ratio_se * se_loss

                    t2 = time.time()
                    dur2.append(t2 - t1)
                    # autograd
                    optimizer.zero_grad()
                    train_total_loss_batch.backward()
                    # clip_grad_norm_(itertools.chain(main_net.parameters(), drug_net.parameters(), te_net.parameters(), se_net.parameters()), max_norm=10, norm_type=2)
                    optimizer.step()
                    t3 = time.time()
                    dur3.append(t3 - t2)
                    if iteration % 100 == 0:
                        print(
                            'Epoch {:05d} | Iteration {:05d} | Train_Loss {:.4f} | Time1(s) {:.4f} | Time2(s) {:.4f} | Time3(s) {:.4f}'.format(
                                epoch, iteration, train_total_loss_batch.item(), np.mean(dur1), np.mean(dur2), np.mean(dur3)))

                main_net.eval()
                drug_net.eval()
                se_net.eval()
                te_net.eval()
                val_te_loss, val_se_loss, val_total_loss=[],[],[]
                with torch.no_grad():
                    for iteration in range(val_sample_idx_generator.num_iterations()):
                        val_sample_idx_batch = val_sample_idx_generator.next()
                        val_drug_drug_batch = val_drug_drug_samples[val_sample_idx_batch]
                        val_drug_drug_batch_ = val_drug_drug_batch[:,[1,0,2]]
                        val_drug_drug_batch_combined = np.concatenate([val_drug_drug_batch,val_drug_drug_batch_],axis=0).tolist()

                        val_te_labels_batch = val_te_labels[val_sample_idx_batch]
                        val_se_labels_batch = val_se_labels[val_sample_idx_batch]

                        val_drug_drug_idx = (np.array(val_drug_drug_batch_combined)[:, :-1].astype(int)).tolist()
                        val_cellline_symbol = (np.array(val_drug_drug_batch_combined)[:, -1]).tolist()
                        val_cellline_idx = [cellline2id_dict[i] for i in val_cellline_symbol]

                        val_g_lists, val_indices_lists, val_idx_batch_mapped_lists = parse_minibatch(
                            adjlists_ua, edge_metapath_indices_list_ua, val_drug_drug_idx, device, neighbor_samples,
                            no_masks, num_drug)

                        [row_drug_embedding, col_drug_embedding], _, [row_drug_atten, col_drug_atten] = main_net(
                            (val_g_lists, features_list, type_mask[:num_drug + num_target],
                             val_indices_lists, val_idx_batch_mapped_lists))

                        row_drug_batch, col_drug_batch = np.array(val_drug_drug_idx)[:, 0], np.array(val_drug_drug_idx)[:, 1]

                        drug_struc_input, drug_idx = iter(drug_struc_loader).next()
                        drug_atom_num = drug_struc_input.ndata['atom_num'].clone().detach().numpy()
                        drug_atom_num = torch.LongTensor([atomnum2id_dict[i] for i in drug_atom_num]).to(device)
                        drug_struc_input = drug_struc_input.to(device)
                        drug_struc_embedding = drug_net(drug_struc_input, drug_atom_num)
                        row_drug_struc_embedding, col_drug_struc_embedding = drug_struc_embedding[row_drug_batch], drug_struc_embedding[col_drug_batch]
                        row_drug_composite_embedding = torch.cat((row_drug_embedding, row_drug_struc_embedding), axis=1)
                        col_drug_composite_embedding = torch.cat((col_drug_embedding, col_drug_struc_embedding), axis=1)

                        se_output = sigmoid(se_net(row_drug_composite_embedding, col_drug_composite_embedding))
                        val_cellline_idx = torch.LongTensor(val_cellline_idx).to(device)
                        if output_concat == True:
                            se_output_ = se_output.clone().detach()
                            te_output = te_net(row_drug_composite_embedding, col_drug_composite_embedding, val_cellline_idx, se_output_)
                        else:
                            te_output = te_net(row_drug_composite_embedding, col_drug_composite_embedding, val_cellline_idx)

                        se_output = (se_output[:se_output.shape[0]//2,:] + se_output[se_output.shape[0]//2:,:])/2
                        te_output = (te_output[:te_output.shape[0]//2,:] + te_output[te_output.shape[0]//2:,:])/2

                        te_loss = te_criterion(te_output, val_te_labels_batch)
                        se_loss = se_criterion(se_output, val_se_labels_batch)
                        val_total_loss.append(loss_ratio_te * te_loss + loss_ratio_se * se_loss)

                    val_total_loss=torch.mean(torch.tensor(val_total_loss))
                    VAL_L0SS.append(val_total_loss.item())
                t_end = time.time()
                print('Epoch {:05d} | Val_Loss {:.4f} | Time(s) {:.4f}'.format(
                    epoch, val_total_loss.item(), t_end - t_start))

                scheduler.step()
                early_stopping(val_total_loss,
                               {
                                   'main_net': main_net.state_dict(),
                                   'drug_net': drug_net.state_dict(),
                                   'se_net': se_net.state_dict(),
                                   'te_net': te_net.state_dict()
                               })
                if early_stopping.early_stop:
                    print('Early stopping based on the validation loss!')
                    break

        print('The name of loaded model is:', model_save_path)
        checkpoint=torch.load(model_save_path)
        main_net.load_state_dict(checkpoint['main_net'])
        drug_net.load_state_dict(checkpoint['drug_net'])
        se_net.load_state_dict(checkpoint['se_net'])
        te_net.load_state_dict(checkpoint['te_net'])

        main_net.eval()
        drug_net.eval()
        se_net.eval()
        te_net.eval()
        test_te_results, test_se_results = [], []
        test_te_label_list, test_se_label_list = [], []
        with torch.no_grad():
            for iteration in range(test_sample_idx_generator.num_iterations()):
                test_sample_idx_batch = test_sample_idx_generator.next()
                test_drug_drug_batch = test_drug_drug_samples[test_sample_idx_batch]
                test_drug_drug_batch_ = test_drug_drug_batch[:,[1,0,2]]
                test_drug_drug_batch_combined = np.concatenate([test_drug_drug_batch,test_drug_drug_batch_],axis=0).tolist()

                test_te_labels_batch = test_te_labels[test_sample_idx_batch]
                test_se_labels_batch = test_se_labels[test_sample_idx_batch]
                test_drug_drug_idx = (np.array(test_drug_drug_batch_combined)[:, :-1].astype(int)).tolist()
                test_cellline_symbol = (np.array(test_drug_drug_batch_combined)[:, -1]).tolist()
                test_cellline_idx = [cellline2id_dict[i] for i in test_cellline_symbol]

                test_g_lists, test_indices_lists, test_idx_batch_mapped_lists = parse_minibatch(
                    adjlists_ua, edge_metapath_indices_list_ua, test_drug_drug_idx, device, neighbor_samples,
                    no_masks, num_drug)

                [row_drug_embedding, col_drug_embedding], _, [row_drug_atten, col_drug_atten] = main_net(
                    (test_g_lists, features_list, type_mask[:num_drug + num_target],
                     test_indices_lists, test_idx_batch_mapped_lists))

                row_drug_batch, col_drug_batch = np.array(test_drug_drug_idx)[:, 0], np.array(test_drug_drug_idx)[:, 1]

                drug_struc_input, drug_idx = iter(drug_struc_loader).next()
                drug_atom_num = drug_struc_input.ndata['atom_num'].clone().detach().numpy()
                drug_atom_num = torch.LongTensor([atomnum2id_dict[i] for i in drug_atom_num]).to(device)
                drug_struc_input = drug_struc_input.to(device)
                drug_struc_embedding = drug_net(drug_struc_input, drug_atom_num)
                row_drug_struc_embedding, col_drug_struc_embedding = drug_struc_embedding[row_drug_batch], drug_struc_embedding[col_drug_batch]
                row_drug_composite_embedding = torch.cat((row_drug_embedding, row_drug_struc_embedding), axis=1)
                col_drug_composite_embedding = torch.cat((col_drug_embedding, col_drug_struc_embedding), axis=1)

                se_output = sigmoid(se_net(row_drug_composite_embedding, col_drug_composite_embedding))
                test_cellline_idx = torch.LongTensor(test_cellline_idx).to(device)
                if output_concat == True:
                    se_output_ = se_output.clone().detach()
                    te_output = te_net(row_drug_composite_embedding, col_drug_composite_embedding, test_cellline_idx, se_output_)
                else:
                    te_output = te_net(row_drug_composite_embedding, col_drug_composite_embedding, test_cellline_idx)

                se_output = (se_output[:se_output.shape[0]//2,:] + se_output[se_output.shape[0]//2:,:])/2
                te_output = (te_output[:te_output.shape[0]//2,:] + te_output[te_output.shape[0]//2:,:])/2
                test_te_results.append(te_output)
                test_te_label_list.append(test_te_labels_batch)
                test_se_results.append(se_output)
                test_se_label_list.append(test_se_labels_batch)

            test_te_results = torch.cat(test_te_results)
            test_te_results = test_te_results.cpu().numpy()
            # test_te_results = scaler.inverse_transform(test_te_results)
            test_se_results = torch.cat(test_se_results)
            test_se_results = test_se_results.cpu().numpy()

            test_te_label_list = torch.cat(test_te_label_list)
            test_te_label_list = test_te_label_list.cpu().numpy()
            # test_te_label_list = scaler.inverse_transform(test_te_label_list)
            test_se_label_list = torch.cat(test_se_label_list)
            test_se_label_list = test_se_label_list.cpu().numpy()

        print('the size of test_te_results, test_se_results:', test_te_results.shape, test_se_results.shape)
        print('the size of test_te_label_list, test_se_label_list:', test_te_label_list.shape, test_se_label_list.shape)
        TE_MSE = mean_squared_error(test_te_label_list, test_te_results)
        TE_RMSE = np.sqrt(TE_MSE)
        TE_MAE = mean_absolute_error(test_te_label_list, test_te_results)
        TE_PEARSON = scipy.stats.pearsonr(test_te_label_list.reshape(-1), test_te_results.reshape(-1))

        print('Link Prediction Test')
        print('TE_MSE = {}'.format(TE_MSE))
        print('TE_RMSE = {}'.format(TE_RMSE))
        print('TE_MAE = {}'.format(TE_MAE))
        print('TE_PEARSON and p-value = {},{}'.format(TE_PEARSON[0], TE_PEARSON[1]))

        mse_list.append(TE_MSE)
        rmse_list.append(TE_RMSE)
        mae_list.append(TE_MAE)
        pearson_list.append(TE_PEARSON[0])

    print('----------------------------------------------------------------')
    print('Link Prediction Tests Summary')
    print('MSE_mean = {}, MSE_std = {}'.format(np.mean(mse_list), np.std(mse_list)))
    print('RMSE_mean = {}, RMSE_std = {}'.format(np.mean(rmse_list), np.std(rmse_list)))
    print('MAE_mean = {}, MAE_std = {}'.format(np.mean(mae_list), np.std(mae_list)))
    print('PEARSON_mean = {}, PEARSON_std = {}'.format(np.mean(pearson_list), np.std(pearson_list)))

    pd.DataFrame(VAL_L0SS, columns=['VAL_LOSS']).to_csv(
        root_prefix+'checkpoint/VAL_LOSS.csv')


if __name__ == '__main__':
    # part1 (for meta-path embedding generation)
    ap = argparse.ArgumentParser(description='HNE-GIN-DDI testing for drug-drug link prediction')
    ap.add_argument('--root-prefix', type=str,
                    default='./data/data4training_model/',
                    help='root from which to read the original input files')
    ap.add_argument('--hidden-dim-main', type=int, default=64,
                    help='Dimension of the node hidden state in the main model. Default is 64.')
    ap.add_argument('--num-heads-main', type=int, default=8,
                    help='Number of the attention heads in the main model. Default is 8.')
    ap.add_argument('--attnvec-dim-main', type=int, default=128,
                    help='Dimension of the attention vector in the main model. Default is 128.')
    ap.add_argument('--rnn-type-main', default='bi-gru',
                    help='Type of the aggregator in the main model. Default is bi-gru.')
    ap.add_argument('--epoch', type=int, default=20, help='Number of epochs. Default is 20.')
    ap.add_argument('--patience', type=int, default=8, help='Patience. Default is 8.')
    ap.add_argument('--batch-size', type=int, default=32,
                    help='Batch size. Please choose an odd value, because of the way of calculating val/test labels of our model. Default is 32.')
    ap.add_argument('--samples', type=int, default=100,
                    help='Number of neighbors sampled in the parse function of main model. Default is 100.')
    ap.add_argument('--repeat', type=int, default=1, help='Repeat the training and testing for N times. Default is 1.')
    # if it is set to False, the GAT layer will ignore the feature of the central node itself
    ap.add_argument('--attn-switch-main', default=True,
                    help='whether need to consider the feature of the central node when using GAT layer in the main model')
    ap.add_argument('--rnn-concat-main', default=False,
                    help='whether need to concat the feature extracted from rnn with the embedding from GAT layer in the main model')
    # part2 (for other modules in HNEMA)
    ap.add_argument('--hidden-dim-aux', type=int, default=64,
                    help='Dimension of generated cell line embeddings and drug chemical feature embeddings. Default is 64.')
    ap.add_argument('--loss-ratio-te', type=float, default=1,
                    help='The weight percentage of therapeutic effect loss in the total loss')
    ap.add_argument('--loss-ratio-se', type=float, default=1,
                    help='The weight percentage of adverse effect loss in the total loss')
    ap.add_argument('--layer-list', default=[2048, 1024, 512],
                    help='layer neuron units list for the DNN TE predictor.')
    ap.add_argument('--pred_in_dropout', type=float, default=0.2,
                    help='The dropout rate in the DNN TE predictor')
    ap.add_argument('--pred_out_dropout', type=float, default=0.5,
                    help='The dropout rate in the DNN TE predictor')
    ap.add_argument('--output_concat', default=False,
                    help='Whether put the adverse effect output into therapeutiec effect prediction')

    args = ap.parse_args()
    run_model_HNEMA_DDI(args.root_prefix, args.hidden_dim_main, args.num_heads_main, args.attnvec_dim_main, args.rnn_type_main, args.epoch,
                        args.patience, args.batch_size, args.samples, args.repeat, args.attn_switch_main, args.rnn_concat_main, args.hidden_dim_aux,
                        args.loss_ratio_te, args.loss_ratio_se, args.layer_list, args.pred_in_dropout, args.pred_out_dropout, args.output_concat, args)

